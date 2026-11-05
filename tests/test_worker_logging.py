import subprocess
import sys


def test_worker_log_is_written_even_if_an_import_configured_the_root_logger(tmp_path):
    code='''
import logging,sys
from types import SimpleNamespace
logging.basicConfig(level=logging.WARNING)
class Pilot:
 def __init__(self,settings):pass
 async def run_cloud_worker(self):
  logging.getLogger('seat-events').info('Connection lost; diagnostic logging works')
  open(sys.argv[2]+'/stop','w').close()
sys.modules['schedule_controller.pilot']=SimpleNamespace(Pilot=Pilot)
sys.modules['schedule_controller.pilot_repository']=SimpleNamespace(machine_sites=lambda machine:[1])
sys.argv=['worker','--output',sys.argv[1]]
from poker_controller.worker import main
main()
logging.shutdown()
'''
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert 'Connection lost; diagnostic logging works' in (tmp_path/'worker.log').read_text(encoding='utf-8')
