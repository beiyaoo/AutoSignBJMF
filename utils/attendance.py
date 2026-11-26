"""
签到功能模块
负责执行签到任务
"""

import re
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from .user_info import get_user_and_class_info


ACTIVITY_URL = re.compile(r'/student/(punch\w+|daka)/course/(\d+)/(\d+)')
ACTIVITY_ATTR = re.compile(r'punchcard_(\d+)')


def _activity_candidates(response, class_id, module):
    """Use the activity's actual route when the list page provides one."""
    route = ACTIVITY_URL.fullmatch(urlsplit(response.url).path.rstrip('/'))
    links = list(ACTIVITY_URL.finditer(response.text))
    if route:
        links.insert(0, route)

    routes = [link.groups() for link in links]
    candidates = [(punch_type, activity_id) for punch_type, course_id, activity_id in routes
                  if course_id == str(class_id)]
    linked_ids = {activity_id for _, activity_id in candidates}
    candidates.extend((module, activity_id) for activity_id in ACTIVITY_ATTR.findall(response.text)
                      if activity_id not in linked_ids)
    return candidates


def _has_completed_status(text):
    return any(marker in text for marker in ('已签到', '已经签到', '已完成签到', '已打卡', '已经打卡'))


def _list_shows_completed(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    return any(_has_completed_status(element.get_text())
               for element in soup.select('.punch-success-info, .punch-status'))


def _submission_status(response, require_new_sign_in):
    if response.status_code != 200:
        print(f"签到请求失败，状态码: {response.status_code}")
        return 'error'

    soup = BeautifulSoup(response.text, 'html.parser')
    title = soup.find('div', id='title')
    status_elements = soup.select('.punch-success-info, .punch-status')
    texts = [element.get_text(strip=True) for element in status_elements]
    if title:
        texts.insert(0, title.get_text(strip=True))
    for text in texts:
        if '未开始' in text:
            return 'not_started'
        if any(marker in text for marker in ('已结束', '已截止', '已关闭', '不在签到时间')):
            return 'no_sign_in'
        if any(marker in text for marker in ('失败', '错误', '未登录')):
            return 'error'
        if _has_completed_status(text):
            return 'already_signed'
        if any(marker in text for marker in ('签到成功', '打卡成功')):
            return 'success'

    # Existing callers accept the legacy response format. Independent periods
    # need confirmation so a generic HTTP 200 cannot complete a different slot.
    return 'unconfirmed_sign_in' if require_new_sign_in else 'success'


def Task(student):
    """
    执行签到任务
    
    Args:
        student (dict): 学生配置信息
        
    Returns:
        tuple: (用户名, 签到状态) 
               状态可能为: 'success'(成功), 'already_signed'(已签到), 'not_started'(未开始),
                          'no_sign_in'(无签到), 'no_new_sign_in'(只找到已签到的活动),
                          'unconfirmed_sign_in'(平台未明确返回签到结果),
                          'error'(错误), 'skip'(跳过)
    """
    try:
        # 先获取用户和班级信息
        user_info, class_info = get_user_and_class_info(student)
        
        # 检查是否成功获取了用户信息，如果没有则跳过该用户
        user_name = user_info.get('name', '未找到')
        if user_name == "未找到":
            print(f"无法获取用户信息，可能是cookie过期或无效，跳过用户 {student['name']} 的签到任务")
            return student.get('name', '未知'), 'skip'
            
        # 使用从网页获取的班级ID，如果获取失败则使用配置文件中的
        ClassID = class_info.get('class_id', student['class'])
        if not ClassID or ClassID == "未找到" or not ClassID:
            ClassID = student['class']
            
        # 使用从网页获取的姓名，如果获取失败则使用配置文件中的
        name = user_info.get('name', student['name'])
        if not name or name == "未找到":
            name = student['name']
            
        # 检查是否成功获取了班级信息，如果没有也跳过该用户
        class_name = class_info.get('class_name', '未找到')
        if class_name == "未找到" and not ClassID:
            print(f"无法获取班级信息，跳过用户 {name} 的签到任务")
            return name, 'skip'
            
        lat = student['lat']
        lng = student['lng']
        ACC = student['acc']
        
        # 使用 requests.Session 保持会话
        session = requests.Session()
        
        # 设置Headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; X64; Linux; Android 9;) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Firefox/92.0  WeChat/x86_64 Weixin NetType/4G Language/zh_CN ABI/x86_64',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/wxpic,image/tpg,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        }
        
        # 添加Cookie
        # 优先使用完整cookie字符串，如果格式不标准则尝试提取
        headers['Cookie'] = student['cookie']
        session.headers.update(headers)
        
        # 1. 访问个人中心以触发 remember_me 自动登录并获取 session cookie
        warmup_url = "https://bjmf.k8n.cn/student/my"
        try:
            session.get(warmup_url, timeout=10)
        except Exception as e:
            print(f"会话预热失败: {e}")

        # 2. 探测所有签到/打卡模块的列表页，合并所有签到项
        require_new_sign_in = bool(student.get('require_new_sign_in'))
        candidates = {}
        list_responses = []
        for module in ('punchs', 'daka'):
            list_url = f'https://bjmf.k8n.cn/student/course/{ClassID}/{module}'
            session.headers.update({'Referer': f'https://bjmf.k8n.cn/student/course/{ClassID}'})

            try:
                response = session.get(list_url, timeout=10)
            except Exception as e:
                print(f"获取 {module} 列表页失败: {e}")
                continue

            if response.status_code == 200:
                list_responses.append(response)
                for candidate in _activity_candidates(response, ClassID, module):
                    candidates.setdefault(candidate, None)

        if not list_responses:
            print("无法取得签到活动列表")
            return name, 'error'

        outcomes = set()
        for punch_type, activity_id in candidates:
            print(f"签到项: {activity_id} (模块: {punch_type})")
            activity_url = f"https://bjmf.k8n.cn/student/{punch_type}/course/{ClassID}/{activity_id}"
            payload = {
                'id': activity_id,
                'lat': lat,
                'lng': lng,
                'acc': ACC,
                'res': '',
                'gps_addr': ''
            }

            try:
                response = session.post(activity_url, data=payload, timeout=10)
            except requests.RequestException as exc:
                print(f"签到项 {activity_id} 请求失败: {exc}")
                outcomes.add('error')
                continue

            outcome = _submission_status(response, require_new_sign_in)
            if outcome == 'success':
                print("本次签到成功")
                return name, 'success'
            outcomes.add(outcome)

        if not candidates and any(_list_shows_completed(response) for response in list_responses):
            outcomes.add('already_signed')
        if 'already_signed' in outcomes and not require_new_sign_in:
            return name, 'already_signed'
        for outcome in ('unconfirmed_sign_in', 'error', 'not_started'):
            if outcome in outcomes:
                return name, outcome
        if 'already_signed' in outcomes:
            return name, 'no_new_sign_in'
        print("未找到在进行的签到/不在签到时间内")
        return name, 'no_sign_in'
    except Exception as e:
        print(f"发生错误{e}，跳过该配置......")
        return student.get('name', '未知'), 'error'
