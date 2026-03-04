import os, json
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from openai import OpenAI
from tavily import TavilyClient

# 1. 初始化配置
# 环境变量需在 GitHub Secrets 中配置
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
TAVILY_KEY = os.getenv("TAVILY_KEY")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")

client = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
tavily = TavilyClient(api_key=TAVILY_KEY)

# 2. 核心：意图识别逻辑 (方案一)
def judge_need_search(query):
    try:
        # 使用极简 Prompt，强制模型只输出一个词，节省 Token 到了极致
        check_res = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是一个意图判定专家。如果用户问题涉及实时新闻、事实查证、最新数据、天气股价等需要联网的信息，请回答'YES'，否则回答'NO'。严禁回答其他内容。"},
                {"role": "user", "content": query}
            ],
            max_tokens=5, # 严格限制输出长度
            temperature=0
        )
        return "YES" in check_res.choices[0].message.content.upper()
    except Exception as e:
        print(f"意图判定失败: {e}")
        return False

# 3. 核心：处理用户请求
def get_ai_answer(query):
    # 步骤 A：判定是否需要搜索
    if judge_need_search(query):
        print(f"--- 意图识别：需要联网搜索 ---")
        # 执行 Tavily 搜索
        search_res = tavily.search(query=query, max_results=3)
        context = "\n".join([f"来源: {r['url']}\n内容: {r['content']}" for r in search_res['results']])
        
        # 组装带背景资料的 Prompt
        final_prompt = f"请参考以下资料回答：{query}\n\n资料库：\n{context}"
    else:
        print(f"--- 意图识别：直接对话 ---")
        final_prompt = query

    # 步骤 B：获取最终回答
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": final_prompt}]
    )
    return response.choices[0].message.content

# 4. 飞书消息接收回调
def do_p2p_message_received(data: P2pMessageReceivedEvent) -> None:
    # 解析飞书传入的文本
    msg_json = json.loads(data.event.message.content)
    user_query = msg_json.get("text", "")
    
    # 获取 AI 回答
    ai_answer = get_ai_answer(user_query)
    
    # 构造回复请求
    reply_req = ReplyMessageRequest.builder() \
        .message_id(data.event.message.message_id) \
        .request_body(ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": ai_answer}))
            .msg_type("text")
            .build()) \
        .build()
    
    lark_client.im.v1.message.reply(reply_req)

# 5. 启动飞书长连接客户端
lark_client = lark.Client.builder() \
    .app_id(FEISHU_APP_ID) \
    .app_secret(FEISHU_APP_SECRET) \
    .build()

event_handler = lark.EventDispatcher.builder("", "") \
    .register_p2p_message_received_event(do_p2p_message_received) \
    .build()

def main():
    print("AI 助理已启动，正在通过 WebSocket 监听飞书消息...")
    ws_client = lark.WsClient(FEISHU_APP_ID, FEISHU_APP_SECRET, event_handler, lark.LogLevel.INFO)
    ws_client.start()

if __name__ == "__main__":
    main()
