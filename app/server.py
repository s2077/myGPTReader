import re
import os
import requests
from urllib.parse import urlparse
from flask import Flask, request
from flask_apscheduler import APScheduler
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
import concurrent.futures
from app.daily_hot_news import *
from app.gpt import get_answer_from_chatGPT, get_answer_from_llama_file, get_answer_from_llama_web, index_cache_file_dir
from app.slash_command import register_slack_slash_commands
from app.util import md5

class Config:
    SCHEDULER_API_ENABLED = True

executor = concurrent.futures.ThreadPoolExecutor(max_workers=20)

schedule_channel = "#daily-news"

app = Flask(__name__)

slack_app = App(
    token=os.environ.get("SLACK_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)
slack_handler = SlackRequestHandler(slack_app)

scheduler = APScheduler()
scheduler.api_enabled = True
scheduler.init_app(app)

def send_daily_news(client, news):
    for news_item in news:
        client.chat_postMessage(
            channel=schedule_channel,
            text="",
            blocks=news_item,
            reply_broadcast=True
        )

@scheduler.task('cron', id='daily_news_task', hour=1, minute=30)
def schedule_news():
   zhihu_news = build_zhihu_hot_news_blocks()
   v2ex_news = build_v2ex_hot_news_blocks()
   onepoint3acres_news = build_1point3acres_hot_news_blocks()
   reddit_news = build_reddit_news_hot_news_blocks()
   hackernews_news = build_hackernews_news_hot_news_blocks()
   producthunt_news = build_producthunt_news_hot_news_blocks()
   xueqiu_news = build_xueqiu_news_hot_news_blocks()
   jisilu_news = build_jisilu_news_hot_news_blocks()
   send_daily_news(slack_app.client, [zhihu_news, v2ex_news, onepoint3acres_news, reddit_news, hackernews_news, producthunt_news, xueqiu_news, jisilu_news])

@app.route("/slack/events", methods=["POST"])
def slack_events():
    return slack_handler.handle(request)

def insert_space(text):

    # Handling the case between English words and Chinese characters
    text = re.sub(r'([a-zA-Z])([\u4e00-\u9fa5])', r'\1 \2', text)
    text = re.sub(r'([\u4e00-\u9fa5])([a-zA-Z])', r'\1 \2', text)

    # Handling the situation between numbers and Chinese
    text = re.sub(r'(\d)([\u4e00-\u9fa5])', r'\1 \2', text)
    text = re.sub(r'([\u4e00-\u9fa5])(\d)', r'\1 \2', text)

    # handling the special characters
    text = re.sub(r'([\W_])([\u4e00-\u9fa5])', r'\1 \2', text)
    text = re.sub(r'([\u4e00-\u9fa5])([\W_])', r'\1 \2', text)

    text = text.replace('  ', ' ')

    return text

thread_message_history = {}
MAX_THREAD_MESSAGE_HISTORY = 10

def update_thread_history(thread_ts, message_str=None, urls=None, file=None):
    if urls is not None:
        thread_message_history[thread_ts]['context_urls'].update(urls)
    if message_str is not None:
        if thread_ts in thread_message_history:
            dialog_texts = thread_message_history[thread_ts]['dialog_texts']
            dialog_texts.append(message_str)
            if len(dialog_texts) > MAX_THREAD_MESSAGE_HISTORY:
                dialog_texts = dialog_texts[-MAX_THREAD_MESSAGE_HISTORY:]
            thread_message_history[thread_ts]['dialog_texts'] = dialog_texts
        else:
            thread_message_history[thread_ts]['dialog_texts'] = [message_str]
    if file is not None:
        thread_message_history[thread_ts]['file'] = file

def extract_urls_from_event(event):
    urls = set()
    for block in event['blocks']:
        for element in block['elements']:
            for e in element['elements']:
                if e['type'] == 'link':
                    url = urlparse(e['url']).geturl()
                    urls.add(url)
    return list(urls)

whitelist_file = "app/data//vip_whitelist.txt"

filetype_extension_allowed = ['epub', 'pdf', 'text', 'docx', 'markdown']

def is_authorized(user_id: str) -> bool:
    with open(whitelist_file, "r") as f:
        return user_id in f.read().splitlines()
    
def dialog_context_keep_latest(dialog_texts, max_length=1):
    if len(dialog_texts) > max_length:
        dialog_texts = dialog_texts[-max_length:]
    return dialog_texts

@slack_app.event("app_mention")
def handle_mentions(event, say, logger):
    logger.info(event)

    user = event["user"]
    thread_ts = event["ts"]

    file_md5_name = None

    if event.get('files'):
        if not is_authorized(event['user']):
            say(f'<@{user}>, this feature is only allowed by whitelist user, please contact the admin to open it.', thread_ts=thread_ts)
            return
        file = event['files'][0] # only support one file for one thread
        logger.info('=====> Received file:')
        logger.info(file)
        filetype = file["filetype"]
        if filetype not in filetype_extension_allowed:
            say(f'<@{user}>, this filetype is not supported, please upload a file with extension [{", ".join(filetype_extension_allowed)}]', thread_ts=thread_ts)
            return
        url_private = file["url_private"]
        temp_file_path = index_cache_file_dir + user
        if not os.path.exists(temp_file_path):
            os.makedirs(temp_file_path)
        temp_file_filename = temp_file_path + '/' + file["name"]
        with open(temp_file_filename, "wb") as f:
            response = requests.get(url_private, headers={"Authorization": "Bearer " + slack_app.client.token})
            f.write(response.content)
            logger.info(f'=====> Downloaded file to save {temp_file_filename}')
            temp_file_md5 = md5(temp_file_filename)
            file_md5_name = index_cache_file_dir + temp_file_md5 + '.' + filetype
            if not os.path.exists(file_md5_name):
                logger.info(f'=====> Rename file to {file_md5_name}')
                os.rename(temp_file_filename, file_md5_name)

    parent_thread_ts = event["thread_ts"] if "thread_ts" in event else thread_ts
    if parent_thread_ts not in thread_message_history:
        thread_message_history[parent_thread_ts] = { 'dialog_texts': [], 'context_urls': set(), 'file': None}

    if "text" in event:
        update_thread_history(parent_thread_ts, 'User: %s' % insert_space(event["text"].replace('<@U04TCNR9MNF>', '')), extract_urls_from_event(event))

    if file_md5_name is not None:
        update_thread_history(parent_thread_ts, None, None, file_md5_name)
    
    urls = thread_message_history[parent_thread_ts]['context_urls']
    file = thread_message_history[parent_thread_ts]['file']

    logger.info('=====> Current thread conversation messages are:')
    logger.info(thread_message_history[parent_thread_ts])

    # TODO: https://github.com/jerryjliu/llama_index/issues/778
    # if it can get the context_str, then put this prompt into the thread_message_history to provide more context to the chatGPT
    if file is not None:
        future = executor.submit(get_answer_from_llama_file, dialog_context_keep_latest(thread_message_history[parent_thread_ts]['dialog_texts']), file)
    elif len(urls) > 0: # if this conversation has urls, use llama with all urls in this thread
        future = executor.submit(get_answer_from_llama_web, thread_message_history[parent_thread_ts]['dialog_texts'], list(urls))
    else:
        future = executor.submit(get_answer_from_chatGPT, thread_message_history[parent_thread_ts]['dialog_texts'])

    try:
        gpt_response = future.result(timeout=300)
        update_thread_history(parent_thread_ts, 'AI: %s' % insert_space(f'{gpt_response}'))
        logger.info(gpt_response)
        say(f'<@{user}>, {gpt_response}', thread_ts=thread_ts)
    except concurrent.futures.TimeoutError:
        future.cancel()
        err_msg = 'Task timedout(5m) and was canceled.'
        logger.warning(err_msg)
        say(f'<@{user}>, {err_msg}', thread_ts=thread_ts)

register_slack_slash_commands(slack_app)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True)