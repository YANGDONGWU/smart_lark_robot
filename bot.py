import os, json
import lark_oapi as lark
# 确保导入最新的 im v1 接口类
from lark_oapi.api.im.v1 import *
from openai import OpenAI
from tavily import TavilyClient

# 1. 初始化配置 (从 GitHub Secrets 读取)
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")

client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
tavily = TavilyClient(api_key=TAVILY_KEY)

# 2. 意图识别逻辑
def judge_need_search(query):
    try:
        check_res = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一个意图判定专家。如果用户问题涉及实时新闻、事实查证、最新数据、天气股价等需要联网的信息，请回答'YES'，否则回答'NO'。严禁回答其他内容。"},
                {"role": "user", "content": query}
            ],
            max_tokens=5,
            temperature=0
        )
        return "YES" in check_res.choices[0].message.content.upper()
    except Exception as e:
        print(f"意图判定出错: {e}")
        return False

# 3. 获取 AI 回答逻辑
def get_ai_answer(query):
    if judge_need_search(query):
        print(f"--- 触发联网搜索 ---")
        try:
            search_res = tavily.search(query=query, max_results=3)
            context = "\n".join([f"来源: {r['url']}\n内容: {r['content']}" for r in search_res['results']])
            final_prompt = f"请参考以下资料回答：{query}\n\n资料库：\n{context}"
        except Exception as e:
            print(f"搜索失败: {e}")
            final_prompt = query
    else:
        print(f"--- 直接对话 ---")
        final_prompt = query

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": final_prompt}]
    )
    return response.choices[0].message.content

# 4. 修正后的飞书消息接收回调
def do_p2_message_receive_v1(data: P2MessageReceiveV1) -> None:
    # 飞书消息 content 是字符串形式的 JSON，例如 '{"text":"hello"}'
    content_str = data.event.message.content
    try:
        msg_json = json.loads(content_str)
        user_query = msg_json.get("text", "")
    except Exception:
        # 兼容部分特殊格式
        user_query = content_str

    if not user_query:
        return

    print(f"收到用户消息: {user_query}")
    ai_answer = get_ai_answer(user_query)
    
    # 构造回复
    reply_req = ReplyMessageRequest.builder() \
        .message_id(data.event.message.message_id) \
        .request_body(ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": ai_answer}))
            .msg_type("text")
            .build()) \
        .build()
    
    lark_client.im.v1.message.reply(reply_req)

# 5. 初始化飞书客户端
lark_client = lark.Client.builder() \
    .app_id(FEISHU_APP_ID) \
    .app_secret(FEISHU_APP_SECRET) \
    .build()

# 注册处理器
event_handler = lark.EventDispatcher.builder("", "") \
    .register_p2_message_receive_v1(do_p2_message_receive_v1) \
    .build()

def main():
    print("AI 助理启动成功，WebSocket 监听中...")
    # 启动长连接
    ws_client = lark.WsClient(
        FEISHU_APP_ID, 
        FEISHU_APP_SECRET, 
        event_handler, 
        lark.LogLevel.INFO
    )
    ws_client.start()

if __name__ == "__main__":
    main()
