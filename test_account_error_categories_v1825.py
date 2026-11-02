import unittest
from account_service import AccountService
from cloud_api import CloudError


class AccountErrors(unittest.TestCase):
    def test_known_reason_precedes_generic_http_status(self):
        self.assertEqual(AccountService._auth_message(CloudError('private',401,'INVALID_CREDENTIALS')),'账号或密码不正确。')

    def test_categories_do_not_falsely_label_auth_or_server_errors_as_network(self):
        cases=[(401,'','登录已失效'),(403,'','没有此操作权限'),(429,'','过于频繁'),
            (503,'','HTTP 503'),(409,'','资料已变化'),(422,'','HTTP 422'),
            (0,'NETWORK_UNREACHABLE','系统代理'),(0,'NETWORK_TIMEOUT','超时')]
        for status,code,wanted in cases:
            with self.subTest(status=status,code=code):
                text=AccountService._auth_message(CloudError('secret-proof-password-provider-response',status,code))
                self.assertIn(wanted,text);self.assertNotIn('secret',text)
                if status:self.assertNotIn('网络',text)

    def test_unknown_response_does_not_expose_raw_private_message(self):
        text=AccountService._auth_message(CloudError('private account password https://provider/?token=private',0,'UNKNOWN'))
        self.assertEqual(text,'账号操作未能完成，请稍后重试。')


if __name__=='__main__':unittest.main()
