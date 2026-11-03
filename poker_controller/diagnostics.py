"""Start the signed, bundled log collector alongside an older installed updater."""
import logging
import os
from pathlib import Path
import subprocess
import sys

log=logging.getLogger(__name__)


def start_collector(machine_id):
    if sys.platform!='win32' or not getattr(sys,'frozen',False):return None
    # The helper is packaged in the signed worker release, never downloaded from an untrusted URL.
    helper=Path(sys._MEIPASS)/'diagnostics-agent'/'PokerSuite.Updater.exe'
    folder=Path(r'C:\PokerSuite\Updater')
    config=next((folder/name for name in ('agent.protected','agent.json') if (folder/name).is_file()),None)
    if not helper.is_file() or config is None:
        log.warning('Diagnostic collector unavailable for machine %s: helper or enrollment missing',machine_id)
        return None
    child=subprocess.Popen([str(helper),'--diagnostics-only','--config',str(config),'--parent',str(os.getpid())],
        creationflags=subprocess.CREATE_NO_WINDOW,cwd=str(helper.parent))
    log.info('Diagnostic log collector started for machine %s',machine_id)
    return child
