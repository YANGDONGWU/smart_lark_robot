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

processed_messages = set()

# 2. 全局配置
APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")

ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
tavily_client = TavilyClient(api_key=TAVILY_KEY)
lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()


def judge_need_search(query):
    try:
        check_res = ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一个搜索判定器。如果用户询问：股市、股价、收盘、今天新闻、天气、或任何涉及 2024 年之后的事实，必须回答 'YES'。其余情况回答 'NO'。"},
                {"role": "user", "content": query}
            ],
            max_tokens=5,
            temperature=0
        )
        result = check_res.choices[0].message.content.upper()
        return "YES" in result
    except:
        return True # 出错时默认开启搜索，保证用户体验

# 3. 核心功能
def get_ai_answer(query):
    if judge_need_search(query):
        print(f"--- 正在实时联网搜索最新数据 ---")
        try:
            # 增加搜索深度
            search_res = tavily_client.search(query=query, search_depth="advanced", max_results=5)
            context = "\n".join([f"实时资讯: {r['content']}" for r in search_res['results']])

            # 强力引导：消除模型的“自谦”幻觉
            final_prompt = f"""
                当前实时时间: 2026年3月
                你现在拥有实时联网能力，以下是为你搜索到的最新实时资料：
                ---
                {context}
                ---
                请根据上述实时资料回答用户问题：{query}
                注意：直接回答事实，严禁说“我无法联网”或“我的知识截止到某年”。
                """
        except Exception as e:
            print(f"搜索发生异常: {e}")
            final_prompt = query
    else:
        final_prompt = query

    response = ai_client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": final_prompt}]
    )
    return response.choices[0].message.content

# 4. 消息回调处理

def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    msg_id = data.event.message.message_id

    # --- 1. 去重邏輯 ---
    if msg_id in processed_messages:
        return  # 如果處理過，直接退出，不跑後面的 AI
    processed_messages.add(msg_id)

    # 限制快取大小，防止內存溢出（保留最近 100 條即可）
    if len(processed_messages) > 100:
        processed_messages.pop()
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