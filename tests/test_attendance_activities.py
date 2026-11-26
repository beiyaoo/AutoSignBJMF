import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from utils.attendance import Task


HOST = 'https://bjmf.k8n.cn'


def response(text='', path='/student/my', status=200):
    return SimpleNamespace(status_code=status, url=HOST + path, text=text)


class AttendanceActivityTests(unittest.TestCase):
    def setUp(self):
        user_info = patch('utils.attendance.get_user_and_class_info',
                          return_value=({'name': '测试用户'}, {'class_id': '100', 'class_name': '测试班'}))
        user_info.start()
        self.addCleanup(user_info.stop)
        self.session = Mock()
        session = patch('utils.attendance.requests.Session', return_value=self.session)
        session.start()
        self.addCleanup(session.stop)
        self.student = {'name': '测试用户', 'class': '100', 'lat': '12.3', 'lng': '12.3',
                        'acc': '30', 'cookie': 'test-only'}

    def activity_lists(self, punchs='', daka=''):
        self.session.get.side_effect = [response(), response(punchs), response(daka)]

    def test_independent_attempt_continues_from_old_activity_to_new_activity(self):
        # The same ID can appear in another module, and the explicit punchw
        # route should be preferred to the DOM ID's generic punchs route.
        self.activity_lists('<div id="punchcard_10"><a href="/student/punchw/course/100/10">早上</a></div>',
                            '<a href="/student/daka/course/100/10">晚上</a>')
        self.session.post.side_effect = [response('<div id="title">已经签到</div>'),
                                         response('<div id="title">签到成功</div>')]

        self.assertEqual(Task({**self.student, 'require_new_sign_in': True}), ('测试用户', 'success'))
        self.assertEqual([call.args[0] for call in self.session.post.call_args_list],
                         [HOST + '/student/punchw/course/100/10', HOST + '/student/daka/course/100/10'])

    def test_independent_attempt_does_not_complete_from_unscoped_old_status(self):
        self.activity_lists('<div class="punch-success-info">已签到</div>')

        self.assertEqual(Task({**self.student, 'require_new_sign_in': True}), ('测试用户', 'no_new_sign_in'))
        self.session.post.assert_not_called()

    def test_old_activity_keeps_legacy_status_but_does_not_complete_independent_attempt(self):
        for independent, expected_status in ((False, 'already_signed'), (True, 'no_new_sign_in')):
            with self.subTest(independent=independent):
                self.activity_lists('<a href="/student/punchs/course/100/10">早上</a>')
                self.session.post.return_value = response('<div id="title">已签到</div>')

                self.assertEqual(Task({**self.student, 'require_new_sign_in': independent}),
                                 ('测试用户', expected_status))

    def test_independent_attempt_does_not_accept_generic_success_http_status(self):
        self.activity_lists('<a href="/student/punchs/course/100/10">加载异常</a>',
                            '<a href="/student/daka/course/100/11">晚上</a>')
        self.session.post.side_effect = [response('<title>用户中心</title>'),
                                         response('<div id="title">未开始签到</div>')]

        self.assertEqual(Task({**self.student, 'require_new_sign_in': True}), ('测试用户', 'unconfirmed_sign_in'))
        self.assertEqual(self.session.post.call_count, 2)

    def test_failed_activity_request_does_not_prevent_trying_the_next(self):
        self.activity_lists('<a href="/student/punchs/course/100/10">早上</a>'
                            '<a href="/student/punchs/course/200/20">其他班级</a>'
                            '<a href="/student/punchs/course/100/11">晚上</a>')
        self.session.post.side_effect = [requests.Timeout('test'),
                                         response('<div class="punch-success-info">打卡成功</div>')]

        self.assertEqual(Task(self.student), ('测试用户', 'success'))
        self.assertEqual([call.kwargs['data']['id'] for call in self.session.post.call_args_list], ['10', '11'])


if __name__ == '__main__':
    unittest.main()
