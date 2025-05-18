import asyncio
import base64
import os
import threading
import traceback
from typing import Optional, List, Dict, Any
import urllib
from uuid import uuid4
from wsgiref import types
import json
from contextlib import asynccontextmanager

import asyncclick as click
from google import genai
from google.genai import types as genai_types
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from fastapi import FastAPI, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from common.client import A2ACardResolver, A2AClient
from common.types import TaskState
from common.utils.push_notification_auth import PushNotificationReceiverAuth

from dotenv import load_dotenv
load_dotenv("../.env")


# 複数エージェントURLリスト
AGENT_URLS = [
    "http://localhost:10000",
    "http://localhost:10001",
    "http://localhost:10002",
    # 必要に応じて追加
]


class PushNotificationListener:
    def __init__(
        self,
        host,
        port,
        notification_receiver_auth: PushNotificationReceiverAuth,
    ):
        self.host = host
        self.port = port
        self.notification_receiver_auth = notification_receiver_auth
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=lambda loop: loop.run_forever(), args=(self.loop,)
        )
        self.thread.daemon = True
        self.thread.start()

    def start(self):
        try:
            # Need to start server in separate thread as current thread
            # will be blocked when it is waiting on user prompt.
            asyncio.run_coroutine_threadsafe(
                self.start_server(),
                self.loop,
            )
            print('======= push notification listener started =======')
        except Exception as e:
            print(e)

    async def start_server(self):
        import uvicorn

        self.app = Starlette()
        self.app.add_route(
            '/notify', self.handle_notification, methods=['POST']
        )
        self.app.add_route(
            '/notify', self.handle_validation_check, methods=['GET']
        )

        config = uvicorn.Config(
            self.app, host=self.host, port=self.port, log_level='critical'
        )
        self.server = uvicorn.Server(config)
        await self.server.serve()

    async def handle_validation_check(self, request: Request):
        validation_token = request.query_params.get('validationToken')
        print(
            f'\npush notification verification received => \n{validation_token}\n'
        )

        if not validation_token:
            return Response(status_code=400)

        return Response(content=validation_token, status_code=200)

    async def handle_notification(self, request: Request):
        data = await request.json()
        try:
            if not await self.notification_receiver_auth.verify_push_notification(
                request
            ):
                print('push notification verification failed')
                return None
        except Exception as e:
            print(f'error verifying push notification: {e}')
            print(traceback.format_exc())
            return None

        print(f'\npush notification received => \n{data}\n')
        return Response(status_code=200)


async def send_to_agent_(message, client, streaming, use_push_notifications, notification_receiver_host, notification_receiver_port, sessionId, taskId: Optional[str] = None):

    if taskId is None:
        taskId = uuid4().hex

    message = {
        'role': 'user',
        'parts': [
            {
                'type': 'text',
                'text': message,
            }
        ],
    }

    payload = {
        'id': taskId,
        'sessionId': sessionId,
        'acceptedOutputModes': ['text'],
        'message': message,
    }

    if use_push_notifications:
        payload['pushNotification'] = {
            'url': f'http://{notification_receiver_host}:{notification_receiver_port}/notify',
            'authentication': {
                'schemes': ['bearer'],
            },
        }

    taskResult = None
    if streaming:
        response_stream = client.send_task_streaming(payload)
        async for result in response_stream:
            # result_json = result.model_dump_json(exclude_none=True)
            result_json = result.model_dump(exclude_none=True)
            # result_json = result
            print(
                f'stream event => {result_json}'
            )
            message_id = result_json.get('id')
            if (artifact := result_json.get('result', {}).get('artifact', None)) is not None:
            # if (artifacts := result_json.artifacts) is not None:
                parts = artifact.get('parts')
                # parts = artifacts.parts
                if parts is not None:
                    # yield {"messageId": message_id, "parts": [{"text": part["text"]} for part in parts]}
                    yield {"messageId": message_id, "parts": parts}
            else:
                parts = result_json.get('result', {}).get('status', {}).get('message', {}).get('parts')
                # parts = result_json.status.message.parts
                if parts is not None:
                    # result_parts.extend(parts)
                    # yield {"messageId": message_id, "parts": [{"text": part["text"]} for part in parts]}
                    yield {"messageId": message_id, "parts": parts}

        taskResult = await client.get_task({'id': taskId})
    else:
        taskResult = await client.send_task(payload)
        # print(f'\n{taskResult.model_dump_json(exclude_none=True)}')
        data = taskResult.model_dump(exclude_none=True).get("result", {})
        print(f'data: {str(data)[:500]}')
        try:
            message_id = data.get("id", None)
        except Exception as e:
            print(f'error getting message_id: {str(data)[:500]}')
            raise e
        parts = []
        if "artifacts" in data:
            for artifact in data["artifacts"]:
                if "parts" in artifact:
                    parts = artifact["parts"]
                    # for part in artifact["parts"]:
                    #     if "text" in part:
                    #         parts.append({"text": part["text"]})
                    #     elif "file" in part.get("type", ""):
                    #         print(f'part: {str(part)[:500]}')
                    #         if part.get("file", {}).get("mimeType", "").startswith("image/"):
                    #             print(f'part data: {str(part.get("file", {}).get("bytes", ""))[:500]}')
                    #             parts.append({
                    #                 "inline_data": {
                    #                     "mime_type": part["file"]["mimeType"], 
                    #                     "data": part["file"]["bytes"]
                    #                 }
                    #             })
                    #     elif "data" in part and part["data"].get("type") == "form":
        yield {"messageId": message_id, "parts": parts}

    ## if the result is that more input is required, loop again.
    state = TaskState(taskResult.result.status.state)
    if state.name == TaskState.INPUT_REQUIRED.name:
        print('======= input required =======')
        yield {"messageId": message_id, "hidden": True, "parts": [{"text": f"TaskId: {taskId}\nInput required"}]}
    elif state.name == TaskState.COMPLETED.name:
        print('======= completed =======')
        yield {"messageId": message_id, "hidden": True, "parts": [{"text": f"TaskId: {taskId}\nCompleted"}]}
    else:
        print('======= unknown state =======')
        yield {"messageId": message_id, "hidden": True, "parts": [{"text": f"TaskId: {taskId}\nUnknown state"}]}


async def get_all_agents(agent_urls, session, use_push_notifications, push_notification_receiver):
    tool_declarations = []
    functions = {}
    for agent_url in agent_urls:
        card_resolver = A2ACardResolver(agent_url)
        card = card_resolver.get_agent_card()
        notif_receiver_parsed = urllib.parse.urlparse(push_notification_receiver)
        notification_receiver_host = notif_receiver_parsed.hostname
        notification_receiver_port = notif_receiver_parsed.port

        client = A2AClient(agent_card=card)
        if session == 0:
            sessionId = uuid4().hex
        else:
            sessionId = session

        streaming = card.capabilities.streaming

        card_function = card.name.replace(" ", "_")
        function_declaration = {
            "name": card_function,
            "description": f"{card.description}",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to send to the agent",
                    },
                    "taskId": {
                        "type": "string",
                        "description": "The task id to send to the agent. If not provided, a new task id will be generated.",
                    },
                },
                "required": ["message"],
            },
        }
        async def make_send_to_agent(client, streaming, notification_receiver_host, notification_receiver_port, sessionId, use_push_notifications):
            async def send_to_agent(message, taskId: Optional[str] = None):
                return send_to_agent_(message, client, streaming, use_push_notifications, notification_receiver_host, notification_receiver_port, sessionId, taskId)
            return send_to_agent
        send_to_agent = await make_send_to_agent(client, streaming, notification_receiver_host, notification_receiver_port, sessionId, use_push_notifications)
        tool_declarations.append(function_declaration)
        functions[card_function] = send_to_agent

    tool_config = genai_types.ToolConfig(
        function_calling_config=genai_types.FunctionCallingConfig(
            mode="ANY", allowed_function_names=list(functions.keys())
        )
    )
    tools = genai_types.Tool(function_declarations=tool_declarations)
    config = genai_types.GenerateContentConfig(tools=[tools], tool_config=tool_config)
    host_model = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    return {
        "host_agent": host_model,
        "agent_config": config,
        "functions": functions,
    }

session = 0
use_push_notifications = False
push_notification_receiver = 'http://localhost:5000'
host_agent = None
agent_config = None
functions = None

async def get_agent_resources():
    global host_agent, agent_config, functions
    if host_agent is None:
        agent_info = await get_all_agents(AGENT_URLS, session, use_push_notifications, push_notification_receiver)
        host_agent = agent_info["host_agent"]
        agent_config = agent_info["agent_config"]
        functions = agent_info["functions"]
    return {"host_agent": host_agent, "agent_config": agent_config, "functions": functions}

async def main(history):
    resources = await get_agent_resources()
    host_agent = resources["host_agent"]
    agent_config = resources["agent_config"]
    functions = resources["functions"]
    response = host_agent.models.generate_content(
        model="gemini-2.5-flash-preview-04-17",
        config=agent_config, 
        contents=history
    )
    if (function_call:=response.candidates[0].content.parts[0].function_call):
        name = function_call.name
        args = function_call.args
        print(f"Function call: {name} with args: {args}")
        print("-"*100)
        if name in functions:
            stream = await functions[name](**args)
            async for result in stream:
                yield result | {"message_type": "a2a"}
        else:
            print(f"Error: {name} is not a valid function")
            yield {"parts": [{"text": f"Error: {name} is not a valid function"}], "message_type": "chat"}
    else:
        yield {"messageId": uuid4().hex, "parts": [{"text": response.text}], "message_type": "chat"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize agent resources
    global host_agent, agent_config, functions
    agent_info = await get_all_agents(AGENT_URLS, session, use_push_notifications, push_notification_receiver)
    host_agent = agent_info["host_agent"]
    agent_config = agent_info["agent_config"]
    functions = agent_info["functions"]
    yield
    # Shutdown: cleanup could be added here if needed

# FastAPI app
app = FastAPI(lifespan=lifespan)

class ChatRequest(BaseModel):
    history: List[Dict[str, Any]]

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    async def generate():
        async for result in main(request.history):
            yield f"data: {json.dumps(result)}\n\n"
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream"
    )

# @app.websocket("/ws")
# async def websocket_endpoint(websocket: WebSocket):
#     await websocket.accept()
    
#     while True:
#         try:
#             # Receive message from client
#             data = await websocket.receive_json()
#             history = data.get("history", [])
            
#             # Process with main function
#             async for result in main(history):
#                 await websocket.send_json(result)
#         except Exception as e:
#             print(f"WebSocket error: {e}")
#             await websocket.close()
#             break

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
