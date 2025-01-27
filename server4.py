from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import httpx
import json
import os
import logging
from dotenv import load_dotenv
import datetime
import asyncio
from typing import Set, Dict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store the text state and active connections
class TextState:
    def __init__(self):
        self.default_text = "000DEFAULT"
        self.current_text = self.default_text
        self.last_updated = datetime.datetime.now()
        self.active_connections: Set[asyncio.Queue] = set()

    async def broadcast(self, message: dict):
        dead_connections = set()
        for queue in self.active_connections:
            try:
                # Check if message is within 1.3 seconds window
                message_time = datetime.datetime.fromisoformat(message["timestamp"])
                elapsed = (datetime.datetime.now() - message_time).total_seconds()
                if elapsed <= 1.3:
                    await queue.put(message)
            except Exception as e:
                logger.error(f"Error broadcasting message: {e}")
                dead_connections.add(queue)
        
        # Cleanup dead connections
        self.active_connections -= dead_connections

text_state = TextState()

class TTSRequest(BaseModel):
    input: dict
    voice: dict
    audioConfig: dict

class ReceiveText(BaseModel):
    text: str

async def get_access_token():
    """Get access token from API key"""
    api_key = os.getenv("GOOGLE_TTS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Google API key not configured")
    
    url = f"https://eu-texttospeech.googleapis.com/v1beta1/text:synthesize?key={api_key}"
    return url

@app.post("/receive")
async def receive_text(text_data: ReceiveText):
    """Receive text from external source and broadcast to all connected clients"""
    if text_data.text != text_state.current_text:
        text_state.current_text = text_data.text
        text_state.last_updated = datetime.datetime.now()
        
        # Broadcast to all connected clients
        message = {
            "text": text_state.current_text,
            "timestamp": text_state.last_updated.isoformat()
        }
        await text_state.broadcast(message)
        
        logger.info(f"Broadcasted new text: {text_state.current_text}")
        return {"status": "success", "text": text_state.current_text}
    return {"status": "skipped", "message": "Text unchanged"}

@app.post("/api/tts")
async def text_to_speech(request: TTSRequest):
    try:
        # Get URL with API key
        url = await get_access_token()
        
        headers = {
            "Content-Type": "application/json"
        }
        
        # Prepare request payload
        payload = {
            "input": request.input,
            "voice": request.voice,
            "audioConfig": request.audioConfig
        }

        # Make request to Google TTS API
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=30.0
            )
            
            # Check for errors
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Google TTS API error: {response.text}"
                )
            
            return response.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Request to Google TTS API timed out")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"HTTP error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

async def event_generator(request: Request, queue: asyncio.Queue):
    """Generator for SSE events"""
    try:
        while True:
            if await request.is_disconnected():
                break

            message = await queue.get()
            event_data = f"data: {json.dumps(message)}\n\n"
            yield event_data
            queue.task_done()
    except Exception as e:
        logger.error(f"Error in event generator: {e}")
    finally:
        text_state.active_connections.remove(queue)
        logger.info("Client disconnected from SSE")

@app.get("/stream")
async def stream_text(request: Request):
    """SSE endpoint for streaming text updates with 1.3 second window"""
    queue = asyncio.Queue()
    text_state.active_connections.add(queue)

    # Determine initial message based on time window
    now = datetime.datetime.now()
    elapsed = (now - text_state.last_updated).total_seconds()
    
    if elapsed <= 1.3:
        initial_message = {
            "text": text_state.current_text,
            "timestamp": text_state.last_updated.isoformat()
        }
    else:
        initial_message = {
            "text": text_state.default_text,
            "timestamp": now.isoformat()
        }
    
    await queue.put(initial_message)

    return StreamingResponse(
        event_generator(request, queue),
        media_type="text/event-stream",
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Transfer-Encoding': 'chunked'
        }
    )

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.datetime.now().isoformat(),
        "current_text": text_state.current_text,
        "last_updated": text_state.last_updated.isoformat(),
        "active_connections": len(text_state.active_connections)
    }

if __name__ == "__main__":
    import uvicorn
    
    # Get port from environment variable or default to 8000
    port = int(os.getenv("PORT", 10000))
    
    logger.info(f"Starting server on port {port}...")
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info"
    )