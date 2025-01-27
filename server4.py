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

class IPTextState:
    def __init__(self):
        self.default_text = "000DEFAULT"
        self.ip_states: Dict[str, Dict] = {}
        self.ip_connections: Dict[str, Set[asyncio.Queue]] = {}

    def process_text(self, text: str) -> str:
        """Remove periods from text"""
        return text.replace(".", "")

    def get_or_create_ip_state(self, ip: str) -> Dict:
        """Get or create state for an IP address"""
        if ip not in self.ip_states:
            self.ip_states[ip] = {
                "current_text": self.default_text,
                "last_updated": datetime.datetime.now()
            }
            self.ip_connections[ip] = set()
        return self.ip_states[ip]

    async def broadcast(self, ip: str, message: dict):
        """Broadcast message to all connections for a specific IP"""
        if ip not in self.ip_connections:
            return

        dead_connections = set()
        for queue in self.ip_connections[ip]:
            try:
                message_time = datetime.datetime.fromisoformat(message["timestamp"])
                elapsed = (datetime.datetime.now() - message_time).total_seconds()
                if elapsed <= 1.3:
                    await queue.put(message)
            except Exception as e:
                logger.error(f"Error broadcasting message: {e}")
                dead_connections.add(queue)
        
        # Cleanup dead connections
        self.ip_connections[ip] -= dead_connections

text_state = IPTextState()

class TTSRequest(BaseModel):
    input: dict
    voice: dict
    audioConfig: dict

class ReceiveText(BaseModel):
    text: str

def get_client_ip(request: Request) -> str:
    """Get client IP address from request"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0]
    return request.client.host

@app.post("/receive")
async def receive_text(text_data: ReceiveText, request: Request):
    """Receive text from external source and broadcast to connected clients with same IP"""
    client_ip = get_client_ip(request)
    ip_state = text_state.get_or_create_ip_state(client_ip)
    
    if text_data.text != ip_state["current_text"]:
        ip_state["current_text"] = text_data.text
        ip_state["current_text"] = text_state.process_text(ip_state["current_text"])
        ip_state["last_updated"] = datetime.datetime.now()
        
        message = {
            "text": ip_state["current_text"],
            "timestamp": ip_state["last_updated"].isoformat()
        }
        await text_state.broadcast(client_ip, message)
        
        logger.info(f"Broadcasted new text for IP {client_ip}: {ip_state['current_text']}")
        return {"status": "success", "text": ip_state["current_text"]}
    return {"status": "skipped", "message": "Text unchanged"}

async def event_generator(request: Request, queue: asyncio.Queue, client_ip: str):
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
        if client_ip in text_state.ip_connections:
            text_state.ip_connections[client_ip].remove(queue)
        logger.info(f"Client {client_ip} disconnected from SSE")

@app.get("/stream")
async def stream_text(request: Request):
    """SSE endpoint for streaming text updates with 1.3 second window"""
    client_ip = get_client_ip(request)
    ip_state = text_state.get_or_create_ip_state(client_ip)
    
    queue = asyncio.Queue()
    text_state.ip_connections[client_ip].add(queue)

    now = datetime.datetime.now()
    elapsed = (now - ip_state["last_updated"]).total_seconds()
    
    if elapsed <= 1.3:
        initial_message = {
            "text": ip_state["current_text"],
            "timestamp": ip_state["last_updated"].isoformat()
        }
    else:
        initial_message = {
            "text": text_state.default_text,
            "timestamp": now.isoformat()
        }
    
    await queue.put(initial_message)

    return StreamingResponse(
        event_generator(request, queue, client_ip),
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
        "ip_states": {
            ip: {
                "current_text": state["current_text"],
                "last_updated": state["last_updated"].isoformat(),
                "active_connections": len(text_state.ip_connections.get(ip, set()))
            }
            for ip, state in text_state.ip_states.items()
        }
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting server on port {port}...")
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_level="info"
    )
