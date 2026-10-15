"""SMTP 局部路由恢复只在确认场景触发，且保持原邮箱域名证书校验。"""
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from autopilot import smtp_transport as transport


class SMTPTransportTests(unittest.TestCase):
    def test_normal_connection_never_uses_the_direct_fallback(self):
        factory=Mock(return_value='normal')
        with patch.object(transport,'_eligible') as eligible, patch.object(transport,'DirectSMTPSSL') as direct:
            self.assertEqual(transport.connect_ssl('smtp.qq.com',465,factory=factory),'normal')
            eligible.assert_not_called();direct.assert_not_called()

    def test_only_eligible_tls_eof_uses_certificate_verified_sni(self):
        original=ssl.SSLEOFError('proxy TLS EOF')
        with patch.object(transport,'_eligible',return_value=True),patch.object(transport,'DirectSMTPSSL',return_value='direct') as direct:
            self.assertEqual(transport.connect_ssl('smtp.qq.com',465,factory=Mock(side_effect=original)),'direct')
            context=direct.call_args.kwargs['context']
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        with patch.object(transport,'_eligible',return_value=False),patch.object(transport,'DirectSMTPSSL') as direct:
            with self.assertRaises(ssl.SSLEOFError):transport.connect_ssl('smtp.qq.com',465,factory=Mock(side_effect=original))
            direct.assert_not_called()
        with patch.object(transport,'DirectSMTPSSL') as direct:
            with self.assertRaises(ssl.SSLCertVerificationError):
                transport.connect_ssl('smtp.qq.com',465,factory=Mock(side_effect=ssl.SSLCertVerificationError('bad certificate')))
            direct.assert_not_called()

    def test_fake_ip_eligibility_requires_same_host_os_and_existing_proxy(self):
        fake=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('198.18.0.72',465))]
        with patch.object(transport.sys,'platform','darwin'),patch.object(Path,'exists',return_value=True),patch.object(socket,'getaddrinfo',return_value=fake):
            self.assertTrue(transport._eligible('smtp.qq.com',465))
            self.assertFalse(transport._eligible('other.example',465))
            self.assertFalse(transport._eligible('smtp.qq.com',587))
        with patch.object(transport.sys,'platform','darwin'),patch.object(Path,'exists',return_value=True),patch.object(socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',465))]):
            self.assertFalse(transport._eligible('smtp.qq.com',465))

    def test_direct_socket_binds_only_its_socket_and_closes_failed_candidate(self):
        bad,good=Mock(),Mock()
        bad.connect.side_effect=OSError('unreachable')
        with patch.object(transport,'_interface',return_value=4),patch.object(transport,'_addresses',return_value=['1.1.1.1','8.8.8.8']),patch.object(socket,'socket',side_effect=[bad,good]):
            self.assertIs(transport._direct_socket('smtp.qq.com',465,15),good)
            good.setsockopt.assert_called_once_with(socket.IPPROTO_IP,25,4)
            good.connect.assert_called_once_with(('8.8.8.8',465))
            bad.close.assert_called_once();good.close.assert_not_called()

    def test_tls_wrapper_checks_original_host_and_closes_socket_on_failure(self):
        client=object.__new__(transport.DirectSMTPSSL)
        client._host='smtp.qq.com';client.context=Mock()
        sock=Mock();client.context.wrap_socket.side_effect=ssl.SSLCertVerificationError('bad certificate')
        with patch.object(transport,'_direct_socket',return_value=sock):
            with self.assertRaises(ssl.SSLCertVerificationError):client._get_socket('smtp.qq.com',465,15)
        client.context.wrap_socket.assert_called_once_with(sock,server_hostname='smtp.qq.com')
        sock.close.assert_called_once()


if __name__=='__main__':unittest.main()
