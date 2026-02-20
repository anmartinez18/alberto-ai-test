from fastapi import FastAPI, status, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field, ValidationError
from typing import List, Literal
from enum import Enum
import uuid
import httpx
import json
import asyncio
import re

app = FastAPI(title="Notification Service v17 (Technical Test)")

class InputRequest(BaseModel):
    user_input: str
    
class Status(str, Enum):
    queued = "queued"
    processing = "processing"
    sent = "sent"
    failed = "failed"
    
class ChatMessage(BaseModel):
    role: str = Field(default="assistant", example="assistant")
    content: str = Field(..., example="Hello!")

class AIRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., example=[
        {"role": "system", "content": "You are an extractor."},
        {"role": "user", "content": "Send email to test@test.com"}
    ])
    
class Notification(BaseModel):
    to: str = Field(..., example="user@example.com")
    message: str = Field(..., example="Your verification code is 1234")
    type: Literal["email", "sms"] = Field(..., example="email")
    
requests_db: dict[str, dict] = {}
request_lock = asyncio.Lock()

PROVIDER_URL = 'http://localhost:3001'
X_API_KEY = "test-dev-2026"
MAX_RETRIES = 3
RETRY_DELAY = 0.5
  
  
@app.post("/v1/requests", status_code = 201)
async def input_request(request: InputRequest):
    async with request_lock:
        request_id = str(uuid.uuid4())
        requests_db[request_id] = {
            "user_input" : request.user_input,
            "status" : Status.queued.value
        }
    return {"id" : request_id}


async def ai_extract(ai_request: AIRequest, id: str, retries: int):
    try:
        async with httpx.AsyncClient() as client:
            ai_response = await client.post(
                    f"{PROVIDER_URL}/v1/ai/extract", 
                    json=ai_request.model_dump(), 
                    headers={ "X-API-Key": X_API_KEY },
                )
            
            ai_response_content = ai_response.json()["choices"][0]["message"]["content"]

            notification = await validate_json(str(ai_response_content))
            if notification is None:
                if retries > 0:
                    await asyncio.sleep(RETRY_DELAY)
                    return await ai_extract(ai_request, id, retries - 1)
                async with request_lock:
                    requests_db[id]["status"] = Status.failed.value
                return
            
            notify_response = await client.post(
                    f"{PROVIDER_URL}/v1/notify", 
                    json=notification.model_dump(), 
                    headers={ "X-API-Key": X_API_KEY }
                )
        
            async with request_lock:
                if notify_response.status_code == 200:
                    requests_db[id]["status"] = Status.sent.value
                else:
                    requests_db[id]["status"] = Status.failed.value
                    
    except Exception as e:
        async with request_lock:
            requests_db[id]["status"] = Status.failed.value
        print(f"Error for id: {id}: {e}")
                        

async def validate_json(content: str) -> Notification | None:
    try:
        text_no_spaces = re.sub(r'[\n\r\t]+', ' ', content)
        text_with_quotes = re.sub(r'(\w+)[ ]*:', r'"\1":', text_no_spaces)
        json_match = re.search(r'\{.*?\}', text_with_quotes)
        if json_match is not None:
            content_json = json_match.group()
            content_json_clean = re.sub(r"'",'"', content_json)
            content_dict = json.loads(content_json_clean)
            json_clean = json.dumps({k.lower(): v for k, v in content_dict.items()}, ensure_ascii=False)
            return Notification.model_validate_json(json_clean)
        else:
            raise ValueError("No JSON valid found")
       
    except (json.JSONDecodeError, ValidationError, ValueError):
        return None



@app.post("/v1/requests/{id}/process", status_code = 200)
async def process_request(id: str, background_tasks: BackgroundTasks ):
    async with request_lock:
        if id not in requests_db:
            raise HTTPException(status_code=404, detail="Request ID not found")
        
        request = requests_db[id]
        user_input = request["user_input"]
        requests_db[id]["status"] = Status.processing.value
        
    system_prompt = ChatMessage(
        role="system",
        content="""
            You are an information extractor. 
            Extract the destination, message and type (email or sms).
            You must respond only with a JSON with exactly these fields:
            {
                "to" : string (destination),
                "message" : string,
                "type" : "email" | "sms"
            }
            Do not include markdowns, explanations or additional information.
            """
    )
    
    user_prompt = ChatMessage(
        role = "user",
        content = user_input
    )
    
    ai_request = AIRequest(messages=[system_prompt, user_prompt])
    background_tasks.add_task(ai_extract, ai_request, id, MAX_RETRIES)
    

        
@app.get("/v1/requests/{id}", status_code=200)
async def get_status(id: str):
    async with request_lock:
        if id not in requests_db:
            raise HTTPException(status_code=404, detail="Request ID not found")
        
    return {"id": id, "status": requests_db[id]["status"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)