'Validate genuine new owned pre-ACK role observations, never rewrite argv/path.'
from pathlib import Path
import hashlib,json,sys,os,stat
OUT=Path(__file__).resolve().parent
BASE=OUT.parent
PRIVATE=BASE/'managed-execution/source/backend/.venv'
def main():
 selection=json.loads(Path(sys.argv[1]).read_bytes());pins=json.loads(Path(sys.argv[2]).read_bytes())
 if set(selection)!={'schema_guard','workspace_helper','backend'}:raise ValueError('exact_three_owned_observation_selection_required')
 for path,digest in pins.items():
  if Path(path).parent!=BASE/'bootstrap' or hashlib.sha256(Path(path).read_bytes()).hexdigest()!=digest:raise ValueError('exact_final_helpers_required')
 roles=json.loads((OUT/'input-template-r2.json').read_bytes())['context']['python_roles']
 result={}
 for role in roles:
  record=selection[role['name']]
  if set(record)!={'path','sha256','pid','start'}:raise ValueError('closed_actual_observation_selection_required')
  path=Path(record['path']);info=path.lstat()
  if path.resolve(strict=True)!=path or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.getuid() or info.st_nlink!=1:raise ValueError('private_actual_receipt_required')
  raw=path.read_bytes()
  if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('actual_observation_hash_changed')
  v=json.loads(raw)
  if v['pid']!=record['pid'] or v['start']!=record['start']:raise ValueError('actual_owned_receipt_identity_changed')
  if (v['role']!=role['name'] or v['argv']!=role['argv'] or v['cwd']!=role['cwd'] or v['application_imported'] is not False
      or v['startup']['dont_write_bytecode'] is not True or v['startup']['no_site']!=0):raise ValueError('actual_managed_role_census_mismatch')
  if role['name']=='backend' and (v['startup']['prefix']!=str(PRIVATE) or v['startup']['exec_prefix']!=str(PRIVATE)
      or v['startup']['virtualenv_origin']!=str(PRIVATE/'lib/python3.12/site-packages/_virtualenv.py')):raise ValueError('actual_private_uv_prefix_required')
  result[role['name']]={'application_imported':False,'python_realpath':v['startup']['python_realpath'],'argv':v['argv'],'cwd':v['cwd'],'startup_path':v['startup']['sys_path'],'startup':v['startup'],'raw_observation_sha256':hashlib.sha256(raw).hexdigest(),'binding_kind':'genuine_owned_pre_ACK_observation_not_projected_old_census'}
 print(json.dumps(result,sort_keys=True))
if __name__=='__main__':main()
