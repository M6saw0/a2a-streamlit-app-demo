import asyncio
import base64
import os
import threading
import traceback
import urllib

from uuid import uuid4

import asyncclick as click
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response

from common.client import A2ACardResolver, A2AClient
from common.types import TaskState
from common.utils.push_notification_auth import PushNotificationReceiverAuth


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


@click.command()
@click.option('--agent', default='http://localhost:10000')
@click.option('--session', default=0)
@click.option('--history', default=False)
@click.option('--use_push_notifications', default=False)
@click.option('--push_notification_receiver', default='http://localhost:5000')
async def cli(
    agent,
    session,
    history,
    use_push_notifications: bool,
    push_notification_receiver: str,
):
    card_resolver = A2ACardResolver(agent)
    card = card_resolver.get_agent_card()  # get agent card from agent url

    print('======= Agent Card ========')
    print(card.model_dump_json(exclude_none=True))

    notif_receiver_parsed = urllib.parse.urlparse(push_notification_receiver)
    notification_receiver_host = notif_receiver_parsed.hostname
    notification_receiver_port = notif_receiver_parsed.port

    if use_push_notifications:
        notification_receiver_auth = PushNotificationReceiverAuth()
        await notification_receiver_auth.load_jwks(
            f'{agent}/.well-known/jwks.json'
        )

        push_notification_listener = PushNotificationListener(
            host=notification_receiver_host,
            port=notification_receiver_port,
            notification_receiver_auth=notification_receiver_auth,
        )
        push_notification_listener.start()

    client = A2AClient(agent_card=card)
    if session == 0:
        sessionId = uuid4().hex
    else:
        sessionId = session

    continue_loop = True
    streaming = card.capabilities.streaming

    while continue_loop:
        taskId = uuid4().hex
        print('=========  starting a new task ======== ')
        continue_loop = await completeTask(
            client,
            streaming,
            use_push_notifications,
            notification_receiver_host,
            notification_receiver_port,
            taskId,
            sessionId,
        )

        if history and continue_loop:
            print('========= history ======== ')
            task_response = await client.get_task(
                {'id': taskId, 'historyLength': 10}
            )
            print(
                task_response.model_dump_json(
                    include={'result': {'history': True}}
                )
            )


async def completeTask(
    client: A2AClient,
    streaming,
    use_push_notifications: bool,
    notification_receiver_host: str,
    notification_receiver_port: int,
    taskId,
    sessionId,
):
    prompt = click.prompt(
        '\nWhat do you want to send to the agent? (:q or quit to exit)'
    )
    if prompt == ':q' or prompt == 'quit':
        return False

    message = {
        'role': 'user',
        'parts': [
            {
                'type': 'text',
                'text': prompt,
            }
        ],
    }

    file_path = click.prompt(
        'Select a file path to attach? (press enter to skip)',
        default='',
        show_default=False,
    )
    if file_path and file_path.strip() != '':
        with open(file_path, 'rb') as f:
            file_content = base64.b64encode(f.read()).decode('utf-8')
            file_name = os.path.basename(file_path)

        message['parts'].append(
            {
                'type': 'file',
                'file': {
                    'name': file_name,
                    'bytes': file_content,
                },
            }
        )

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
            print(
                f'stream event => {result.model_dump_json(exclude_none=True)}'
            )
        taskResult = await client.get_task({'id': taskId})
    else:
        taskResult = await client.send_task(payload)
        print(f'\n{taskResult.model_dump_json(exclude_none=True)}')
        def print_dict(input_dict, indent=""):
            if isinstance(input_dict, dict):
                for key, value in input_dict.items():
                    if isinstance(value, dict):
                        print_dict(value, indent + "  ")
                    elif isinstance(value, list):
                        print(indent + key + ":---")
                        print_dict(value, indent)
                    else:
                        print(f'{indent}{key}: {str(value)[:50]}')
            elif isinstance(input_dict, list):
                for item in input_dict:
                    if isinstance(item, dict):
                        print_dict(item, indent + "  ")
                    elif isinstance(item, list):
                        print("---")
                        print_dict(item, indent)
                    else:
                        print(f'{indent}{item}')
            else:
                print(f'{indent}{input_dict}')
        print_dict(taskResult.model_dump())

    ## if the result is that more input is required, loop again.
    state = TaskState(taskResult.result.status.state)
    if state.name == TaskState.INPUT_REQUIRED.name:
        print('======= input required =======')
        return await completeTask(
            client,
            streaming,
            use_push_notifications,
            notification_receiver_host,
            notification_receiver_port,
            taskId,
            sessionId,
        )
    ## task is complete
    return True


if __name__ == '__main__':
    asyncio.run(cli())
