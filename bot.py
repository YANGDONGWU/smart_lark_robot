import os
import json
import lark_oapi as lark
# 1. 显式导入模型
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody
)
# 核心修正：导入模块中的 Client 类，并显式指定 EventDispatcher
from lark_oapi.ws import Client as WsClient  # 注意这里是 ws_client 模块里的 Client 类
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
from openai import OpenAI
from tavily import TavilyClient

# 2. 全局配置
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")

ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
tavily_client = TavilyClient(api_key=TAVILY_KEY)
lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

# 3. 核心功能
def get_ai_answer(query):
    try:
        check = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": "Need web search? Reply YES or NO."},
                      {"role": "user", "content": query}],
            max_tokens=5
        )
        should_search = "YES" in check.choices[0].message.content.upper()
    except:
        should_search = False

    if should_search:
        print(f"Searching: {query}")
        search_res = tavily_client.search(query=query, max_results=3)
        context = "\n".join([r['content'] for r in search_res['results']])
        prompt = f"Context: {context}\nQuestion: {query}"
    else:
        prompt = query

    res = ai_client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}]
    )
    return res.choices[0].message.content

# 4. 消息回调处理
def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    try:
        # data.event.message.content 是转义后的字符串 JSON
        content_dict = json.loads(data.event.message.content)
        user_query = content_dict.get("text", "")
    except:
        user_query = ""

    if not user_query:
        return

    print(f"Received: {user_query}")
    answer = get_ai_answer(user_query)

    # 5. 回复消息
    reply_req = ReplyMessageRequest.builder() \
        .message_id(data.event.message.message_id) \
        .request_body(ReplyMessageRequestBody.builder()
                      .content(json.dumps({"text": answer}))
                      .msg_type("text")
                      .build()) \
        .build()

    lark_client.im.v1.message.reply(reply_req)

# 6. 核心修正：使用 EventDispatcher 而非 Handler
event_handler = EventDispatcherHandler.builder("", "") \
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
    .build()

def main():
    print("Bot is starting with lark-oapi 1.5.3...")
    # 7. 实例化 WsClient 类
    ws_client = WsClient(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO
    )
    ws_client.start()

if __name__ == "__main__":
    main()