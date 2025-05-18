import base64
from datetime import datetime
import io
import json
import queue
import time
from PIL import Image
import requests
import asyncio
import threading
from typing import List, Dict, Any, AsyncGenerator

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx


SPINNER = '<div class="spinner-border" role="status"><span class="visually-hidden">Processing...</span></div>'


class A2AApiClient:
    def __init__(self, base_url: str = "http://localhost:8000", max_retries: int = 3, timeout: tuple = (5, 60)):
        self.base_url = base_url
        self.chat_endpoint = f"{self.base_url}/chat"
        self.max_retries = max_retries
        self.timeout = timeout
    
    async def send_message_sse(self, history: List[Dict[str, Any]]) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Send a message to the chat API endpoint and yield responses as they stream in.
        
        Args:
            history: List of message objects in the conversation history
            
        Yields:
            Dictionary containing the response message data
        """
        retries = 0
        while retries <= self.max_retries:
            try:
                response = requests.post(
                    self.chat_endpoint,
                    json={"history": history},
                    stream=True,
                    headers={"Accept": "text/event-stream"},
                    timeout=self.timeout
                )
                
                # Use the response.iter_lines() instead of passing the response object directly
                for line in response.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith('data:'):
                            data = line[5:].strip()
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError as e:
                                print(f"Failed to parse event data: {data}")
                                print(f"Error: {e}")
                
                # If we get here without exceptions, we're done
                break
                
            except requests.exceptions.ChunkedEncodingError:
                retries += 1
                if retries > self.max_retries:
                    print(f"Connection ended prematurely after {self.max_retries} retries.")
                    break
                else:
                    wait_time = retries * 1.5  # Exponential backoff
                    print(f"Connection ended prematurely. Retrying ({retries}/{self.max_retries}) in {wait_time:.1f} seconds...")
                    await asyncio.sleep(wait_time)
                    # Continue to retry
            except requests.exceptions.RequestException as e:
                print(f"Request error in send_message_sse: {e}")
                raise e
            except Exception as e:
                print(f"Error in send_message_sse: {e}")
                raise e


# 初期化時にセッション状態を設定
if "messages" not in st.session_state:
    st.session_state.client = A2AApiClient()
    st.session_state.messages = []
    st.session_state.display_messages = []
    st.session_state.message_id_map = {}
    st.session_state.processing_message = {}
    st.session_state.queue = queue.Queue()
    st.session_state.rerun_queue = queue.Queue()
    st.session_state.backend_process_running = False
    st.session_state.needs_rerun = False


def format_parts_from_a2a(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    formatted_parts = []
    for part in parts:
        if "text" in part:
            formatted_parts.append({"text": part["text"]})
        elif "file" in part.get("type", ""):
            if part.get("file", {}).get("mimeType", "").startswith("image/"):
                formatted_parts.append({
                    "inline_data": {
                        "mime_type": part["file"]["mimeType"], 
                        "data": part["file"]["bytes"]
                    }
                })
        else:
            formatted_parts.append(part)
    return formatted_parts

async def backend_process():
    print("Starting backend process")
    try:
        st.session_state.backend_process_running = True
        connection_success = False
        chat_idx = len(st.session_state.messages)
        st.session_state.messages.append({
            "role": "model",
            "parts": [{"text": "Process started"}]
        })
        st.session_state.display_messages.append({
            "role": "assistant",
            "content": {"text": SPINNER},
        })
        st.session_state.rerun_queue.put(1)
        
        async for response in st.session_state.client.send_message_sse(st.session_state.messages):
            connection_success = True  # 少なくとも1つのレスポンスを受け取った
            st.session_state.backend_process_running = True
            # Print the response data
            # print(f"Received: {response}")
            
            # You can process the response here based on message_type
            if response.get("message_type") == "a2a":
                # Handle A2A message
                message_id = response.get("messageId", None)
                parts = format_parts_from_a2a(response.get("parts", []))
                hidden = response.get("hidden", False)
                if message_id is not None:
                    if message_id not in st.session_state.message_id_map:
                        st.session_state.message_id_map[message_id] = chat_idx
                        st.session_state.messages[chat_idx] = {
                            "role": "model",
                            "parts": [part if "data" not in part else {"text": f"Form data: {part['data']}"} for part in parts]
                        }
                        st.session_state.display_messages[chat_idx] = {
                            "role": "assistant",
                            "content": parts[-1]
                        }
                        st.session_state.processing_message[chat_idx] = True
                        st.session_state.rerun_queue.put(1)
                    else:
                        message_index = st.session_state.message_id_map[message_id]
                        message_ = st.session_state.messages[message_index]
                        if hidden:
                            st.session_state.messages[message_index] = {
                                "role": message_.get("role", "model"),
                                "parts": message_["parts"] + [part if "data" not in part else {"text": f"Form data: {part['data']}"} for part in parts]
                            }
                            # del st.session_state.processing_message[message_index]
                            st.session_state.processing_message[message_index] = False
                            st.session_state.rerun_queue.put(1)
                        else:
                            st.session_state.messages[message_index] = {
                                "role": message_.get("role", "model"),
                                "parts": message_.get("parts", []) + [part if "data" not in part else {"text": f"Form data: {part['data']}"} for part in parts]
                            }
                            st.session_state.display_messages[message_index] = {
                                "role": "assistant",
                                "content": parts[-1]
                            }
                            st.session_state.processing_message[message_index] = True
                            st.session_state.rerun_queue.put(1)
                for part in parts:
                    if "text" in part:
                        print(f"A2A Message: {part['text']}")
                    elif "inline_data" in part and part["inline_data"].get("mime_type", "").startswith("image/"):
                        print(f"A2A Image: {part['inline_data']['data'][:30]}...")
                    elif "data" in part and part["data"].get("type") == "form":
                        print(f"A2A Form: {part['data']['form']}")
                        
            elif response.get("message_type") == "chat":
                # Handle chat message
                parts = format_parts_from_a2a(response.get("parts", []))
                st.session_state.messages[chat_idx] = {
                    "role": "model",
                    "parts": [part if "data" not in part else {"text": f"Form data: {part['data']}"} for part in parts]
                }
                st.session_state.display_messages[chat_idx] = {
                    "role": "assistant",
                    "content": parts[-1]
                }
                st.session_state.rerun_queue.put(1)
                for part in parts:
                    if "text" in part:
                        print(f"Chat Message: {part['text']}")
                    elif "inline_data" in part and part["inline_data"].get("mime_type", "").startswith("image/"):
                        print(f"Chat Image: {message_id}")
                    elif "data" in part and part["data"].get("type") == "form":
                        print(f"Chat Form: {message_id}")
                        

        # もし接続が成功したが応答が受け取れなかった場合
        if not connection_success:
            print("No responses received from API. Check if the server is running correctly.")
            # エラーメッセージを表示
            if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
                st.session_state.messages.append({
                    "role": "model",
                    "parts": [{"text": "サーバーからの応答がありませんでした。後ほど再試行してください。"}]
                })
                st.session_state.display_messages.append({
                    "role": "assistant",
                    "content": {"text": "サーバーからの応答がありませんでした。後ほど再試行してください。"}
                })
                st.session_state.rerun_queue.put(1)
                
    except Exception as e:
        print(f"Error in backend_process: {e}")
        # エラーが発生した場合にメッセージを表示
        if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
            st.session_state.messages.append({
                "role": "model",
                "parts": [{"text": f"エラーが発生しました: {str(e)}"}]
            })
            st.session_state.display_messages.append({
                "role": "assistant",
                "content": {"text": f"エラーが発生しました: {str(e)}"}
            })
            st.session_state.rerun_queue.put(1)
    finally:
        st.session_state.backend_process_running = False


def backend_queue_watcher():
    """Thread that watches st.session_state.queue and runs backend_process for each item."""
    while True:
        try:
            # Wait for a new item in the queue (blocking)
            item = st.session_state.queue.get(block=True)
            
            print("thread start")
            thread = threading.Thread(target=backend_process_thread, daemon=True)
            # Attach the script run context to the threads
            ctx = get_script_run_ctx()
            add_script_run_ctx(thread, ctx)
            # Start threads
            thread.start()
            print("thread end")
        except queue.Empty:
            continue  # No item, just loop again
        except Exception as e:
            print(f"Error in backend_queue_watcher: {e}")
            # Sleep briefly to avoid tight error loops
            time.sleep(1)


def backend_process_thread():
    """Thread that watches st.session_state.queue and runs backend_process for each item."""
    # グローバルなイベントループオブジェクトを作成
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(backend_process())
    loop.close()


def start_backend_queue_thread():
    """Start the backend queue watcher thread if not already started."""
    if "backend_thread_started" not in st.session_state:
        # Create threads
        backend_thread = threading.Thread(target=backend_queue_watcher, daemon=True)
        # rerun_thread = threading.Thread(target=rerun_queue_watcher, daemon=True)
        
        # Attach the script run context to the threads
        ctx = get_script_run_ctx()
        add_script_run_ctx(backend_thread, ctx)
        # add_script_run_ctx(rerun_thread, ctx)
        
        # Start threads
        backend_thread.start()
        # rerun_thread.start()
        
        st.session_state.backend_thread_started = True


def render_dynamic_form(schema: dict, form_data: dict, form_key: str = "dynamic_form", disabled: bool = False):
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    results = {}

    with st.form(key=form_key):
        for field, prop in properties.items():
            field_type = prop.get("type", "string")
            field_format = prop.get("format", "")
            description = prop.get("description", "")
            title = prop.get("title", field)
            value = form_data.get(field, "")

            is_required = field in required

            # 適切なウィジェットを選択
            if field_type == "string" and field_format == "date":
                # 日付
                if value and value != "<transaction date>":
                    try:
                        value = datetime.strptime(value, "%Y-%m-%d").date()
                    except Exception:
                        value = None
                else:
                    value = None
                results[field] = st.date_input(
                    f"{title}{' *' if is_required else ''}",
                    value=value,
                    help=description,
                    key=f"{form_key}_{field}"
                )
            elif field_type == "string" and field_format == "number":
                # 数値（文字列型だけど数値入力）
                try:
                    value = float(value)
                except Exception:
                    value = 0.0
                results[field] = st.number_input(
                    f"{title}{' *' if is_required else ''}",
                    value=value,
                    help=description,
                    key=f"{form_key}_{field}"
                )
            elif field_type == "string":
                # 通常のテキスト
                results[field] = st.text_input(
                    f"{title}{' *' if is_required else ''}",
                    value=value,
                    help=description,
                    key=f"{form_key}_{field}"
                )
            else:
                # その他の型はテキストで
                results[field] = st.text_input(
                    f"{title}{' *' if is_required else ''}",
                    value=str(value),
                    help=description,
                    key=f"{form_key}_{field}"
                )

        submitted = st.form_submit_button("Submit", disabled=disabled)
        if submitted:
            return results
    return None


if "backend_thread_started" not in st.session_state:
    start_backend_queue_thread()


def main():
    # Check for rerun flag at the start
    if st.session_state.get("needs_rerun", False):
        st.session_state.needs_rerun = False
        # We're already in a rerun, just clear the flag and continue
        
    st.title("Chat with A2A API")
    st.markdown("""
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.6/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.6/dist/js/bootstrap.bundle.min.js"></script>
""", unsafe_allow_html=True)
    
    # Add a refresh button
    # if st.button("更新", key="manual_refresh"):
    #     st.rerun()
    
    # Display cha
    forms = {}
    for index, message in enumerate(st.session_state.display_messages):
        role = "user" if message["role"] == "user" else "assistant"
        # メッセージのpartsが空でないことを確認
        if message.get("content"):
            part = message.get("content", {})
            if "text" in part or "inline_data" in part or "data" in part:
                with st.chat_message(role):
                    if st.session_state.processing_message.get(index, False):
                        if "text" in part:
                            st.write(part["text"] + "  \n" + SPINNER, unsafe_allow_html=True)
                        elif "inline_data" in part and part["inline_data"].get("mime_type", "").startswith("image/"):
                            bs64_str = part["inline_data"]["data"]
                            # base64_strをpillowで画像に変換
                            image = Image.open(io.BytesIO(base64.b64decode(bs64_str)))
                            st.image(image)
                        elif "data" in part and part["data"].get("type") == "form":
                            form = render_dynamic_form(part["data"]["form"], part["data"]["form_data"], form_key=f"form_{index}", disabled=part.get("disabled", False))
                            forms[index] = form
                    else:
                        if "text" in part:
                            st.write(part["text"], unsafe_allow_html=True)
                        elif "inline_data" in part and part["inline_data"].get("mime_type", "").startswith("image/"):
                            bs64_str = part["inline_data"]["data"]
                            # base64_strをpillowで画像に変換
                            image = Image.open(io.BytesIO(base64.b64decode(bs64_str)))
                            st.image(image)
                        elif "data" in part and part["data"].get("type") == "form":
                            form = render_dynamic_form(part["data"]["form"], part["data"]["form_data"], form_key=f"form_{index}", disabled=part.get("disabled", False))
                            forms[index] = form
    
    # Handle user input
    if prompt := st.chat_input("Enter a message:"):
        # Display user message
        with st.chat_message("user"):
            st.write(prompt)
        
        # Add to message history
        st.session_state.messages.append({
            "role": "user",
            "parts": [
                {"text": prompt}
            ]
        })
        st.session_state.display_messages.append({
            "role": "user",
            "content": {"text": prompt}
        })
        st.session_state.queue.put(1)
    elif any(forms.values()):
        form_idx = None
        for idx, form_ in forms.items():
            if form_:
                form = form_
                form_idx = idx

        if form_idx is not None:
            with st.chat_message("user"):
                st.write(str(form))
            
            # Add to message history
            st.session_state.messages.append({
                "role": "user",
                "parts": [
                    {"text": str(form)}
                ]
            })
            st.session_state.display_messages.append({
                "role": "user",
                "content": {"text": str(form)}
            })
            st.session_state.display_messages[form_idx]["content"]["disabled"] = True
            st.session_state.queue.put(1)
    else:
        time.sleep(1)
    st.rerun()


if __name__ == "__main__":
    main()
