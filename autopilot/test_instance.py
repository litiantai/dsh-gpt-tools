"""每个项目仅保留一个开发及业务验收测试实例，候选证据独立保存。"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import time


@contextmanager
def slot(state_root, product):
    """串行占用测试实例，替换前只关闭登记且身份一致的旧测试应用。"""
    root = Path(state_root).resolve()
    lock = root/'test-instance-locks'/(product['id']+'.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('测试实例正被开发或业务验收占用，请等待当前检查结束')
        from review_core import Store
        from .store import Ledger
        current = Ledger(Store(root.parent)).get('products', product['id'])
        environment = current.get('test_environment') or {}
        if environment.get('pid') and environment.get('application'):
            application = Path(environment['application']).resolve()
            if not application.is_relative_to(root):
                raise ValueError('登记的测试应用不在隔离目录，禁止关闭')
            pid = int(environment['pid'])
            expected = str(application/'Contents/MacOS/thsoctop')
            def alive():
                proc = subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True)
                command = proc.stdout.strip()
                return proc.returncode==0 and (command==expected or command.startswith(expected+' '))
            if alive():
                os.kill(pid, signal.SIGTERM)
                end = time.monotonic()+15
                while alive() and time.monotonic()<end:
                    time.sleep(.2)
                if alive():
                    raise ValueError('旧测试实例尚未退出，不能同时启动第二个测试实例')
            if environment.get('runtime') and environment.get('support'):
                from .runtime_recovery import processes
                if not all(Path(environment[k]).resolve().is_relative_to(root) for k in ('runtime','support')):
                    raise ValueError('旧测试 Host 路径不在隔离目录，禁止关闭')
                old={'application':str(application),'runtime':environment['runtime'],'app_support':environment['support']}
                for host_pid in processes(old)['hosts']:
                    if host_pid in processes(old)['hosts']:
                        try:
                            os.kill(host_pid,signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                end=time.monotonic()+15
                while processes(old)['hosts'] and time.monotonic()<end:
                    time.sleep(.2)
                if processes(old)['hosts']:
                    raise ValueError('旧测试 Host 尚未退出，不能同时启动第二个测试实例')
        yield


def record(state_root, product_id, environment):
    from review_core import Store
    from .store import Ledger
    ledger = Ledger(Store(Path(state_root).parent))
    with ledger.store.transaction() as db:
        product = ledger.get('products', product_id, db)
        ledger.update('products', product_id, product['version'],
            {'test_environment': environment | {'instance_role':'test'}}, db=db)
