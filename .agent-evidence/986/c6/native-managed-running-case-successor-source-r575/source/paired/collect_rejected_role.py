"""ONE rejected native role observation; no ACK, helper main, SQL or signals."""
import argparse,hashlib,importlib.util,json,os,stat,subprocess,time,fcntl
from pathlib import Path
BASE=Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12')
FIXTURE=BASE/'managed-execution'
OUT=Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12')
ROLE_CONTEXTS={'schema_guard': '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/schema_guard-context.json', 'workspace_helper': '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/workspace_helper-context.json', 'backend': '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/backend-context.json'}
MANAGE_SHA='213869f572840802565b09299310b4ea8bb39b416a0e919d37e9fe99e7d1bc7c'
PIDFD_COMPAT_SHA='e8b7160de15c1020e7dbbf97abf364c2bf6cf34c5f467b81208bab4bc459289e'
def read(path,pin=None):
 path=Path(path);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  before=os.fstat(fd)
  if path.resolve(strict=True)!=path or not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or stat.S_IMODE(before.st_mode) not in ((0o600,0o700) if str(path).startswith(str(FIXTURE/'source')+'/') else (0o600,)):raise RuntimeError('private_input_required')
  raw=b''
  while True:
   chunk=os.read(fd,1048576)
   if not chunk:break
   raw+=chunk
   if len(raw)>16*1024*1024:raise RuntimeError('input_bound')
  after=os.fstat(fd)
  if any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')):raise RuntimeError('input_identity_changed')
  if pin is not None and hashlib.sha256(raw).hexdigest()!=pin:raise RuntimeError('input_hash_changed')
  return raw
 finally:os.close(fd)
def verify_cache(path,digest,record):
 path=Path(path)
 if path.resolve(strict=True)!=path or not path.is_relative_to(FIXTURE/'source/backend/.venv'):raise RuntimeError('exact_private_cache_path_required')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  before=os.fstat(fd);actual={'device':before.st_dev,'inode':before.st_ino,'mode':before.st_mode,'uid':before.st_uid,'nlink':before.st_nlink}
  if actual!=record or before.st_uid!=os.getuid() or before.st_nlink!=1 or not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) not in (0o664,0o775):raise RuntimeError('sealed_copied_cache_metadata_changed')
  result=hashlib.sha256();total=0
  while True:
   raw=os.read(fd,1048576)
   if not raw:break
   result.update(raw);total+=len(raw)
  after=os.fstat(fd)
  if total!=before.st_size or result.hexdigest()!=digest or any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')):raise RuntimeError('sealed_copied_cache_bytes_changed')
 finally:os.close(fd)
def write(path,value):
 raw=(json.dumps(value,sort_keys=True)+'\n').encode();fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
 with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
def start(pid):return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19])
def main():
 parser=argparse.ArgumentParser();parser.add_argument('role',choices=('schema_guard','workspace_helper','backend'));parser.add_argument('--context-sha256',required=True);parser.add_argument('--helper-pins-sha256',required=True);args=parser.parse_args()
 context_path=Path(ROLE_CONTEXTS[args.role]);context=json.loads(read(context_path,args.context_sha256));pins=json.loads(read(BASE/'HELPER-PINS.json',args.helper_pins_sha256))
 generated=BASE/'bootstrap/PINS.txt'
 expected_entries=set(pins)|{str(generated)}
 if len(pins)!=37 or any(Path(path).parent!=BASE/'bootstrap' for path in pins) or {str(p) for p in (BASE/'bootstrap').iterdir()}!=expected_entries:raise RuntimeError('exact_bootstrap_plus_generated_pins_frontier_required')
 expected_bytes=''.join(pin+'  '+Path(path).name+'\n' for path,pin in sorted(pins.items())).encode()
 if read(generated)!=expected_bytes:raise RuntimeError('generated_pins_serialization_changed')
 for path,digest in pins.items():read(path,digest)
 for path,digest in pins.items():
  if context['source_file_pins'].get(path)!=digest:raise RuntimeError('context_helper_pin_changed')
 if context['negative_startup_role']!=args.role or context['source_root']!=str(FIXTURE/'source'):raise RuntimeError('exact_negative_context_required')
 validator=OUT/'negative_context_binding.py'
 if hashlib.sha256(validator.read_bytes()).hexdigest()!='abfddd557b61f193e20564aca85217ec22019336943645c7d4fd41ea6854159c':raise RuntimeError('negative_validator_changed')
 spec=importlib.util.spec_from_file_location('_negative_context',validator);binding=importlib.util.module_from_spec(spec);spec.loader.exec_module(binding)
 dotenvpath=BASE/'bootstrap/managed_fixture_dotenv.py'
 read(dotenvpath,pins[str(dotenvpath)]);spec=importlib.util.spec_from_file_location('_negative_dotenv',dotenvpath);dotenv=importlib.util.module_from_spec(spec);spec.loader.exec_module(dotenv)
 binding.validate(context,context['launcher_env'],context['source_root'],dotenv.read_dotenv(context['dotenv']),hashlib.sha256(read(Path(context['dotenv']))).hexdigest())
 role=next(r for r in context['python_roles'] if r['name']==args.role)
 if len(context['python_roles'])!=3 or sorted(r['name'] for r in context['python_roles'])!=['backend','schema_guard','workspace_helper']:raise RuntimeError('exact_three_original_roles_required')
 if role['startup_path'] is not None:raise RuntimeError('negative_observation_must_not_claim_vector')
 source=Path(context['source_root']);read(source/'manage.sh',MANAGE_SHA)
 for path,digest in context['source_file_pins'].items():
  if path in pins:continue
  if str(path).startswith(str(source/'backend/.venv')+'/'):verify_cache(path,digest,context['negative_copy_metadata'][path])
  else:read(path,digest)
 # Exact declared regular copied-cache frontier, plus original four aliases.
 cache=source/'backend/.venv'
 regular={str(p) for p in cache.rglob('*') if p.is_file() and not p.is_symlink()}
 if regular!=set(context['negative_copy_metadata']):raise RuntimeError('copied_cache_frontier_changed')
 aliases={'bin/python':'/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu/bin/python3.12','bin/python3':'python','bin/python3.12':'python','lib64':'lib'}
 links={str(p.relative_to(cache)):os.readlink(p) for p in cache.rglob('*') if p.is_symlink()}
 if links!=aliases:raise RuntimeError('copied_cache_four_aliases_changed')
 # Original R3 must already cover this controller before cached imports.
 status=Path('/proc/self/status').read_text()
 if 'NoNewPrivs:\t1' not in status or 'Seccomp:\t2' not in status:raise RuntimeError('inherited_kernel_denial_required')
 for standard in (0,1,2):
  info=os.fstat(standard);identity={'device':info.st_dev,'inode':info.st_ino,'mode':info.st_mode,'uid':info.st_uid,'nlink':info.st_nlink,'open_flags':fcntl.fcntl(standard,fcntl.F_GETFL),'descriptor_flags':fcntl.fcntl(standard,fcntl.F_GETFD)}
  if identity!=role['fds'][str(standard)]:raise RuntimeError('actual_original_role_stdio_changed')
 # Keep original shell sourcing/default/export semantics, before original child.
 env=dict(context['launcher_env']);env['SERAPH_MANAGED_TEST_CONTEXT']=str(context_path)
 if env['UV_NO_SYNC']!='1' or env['UV_OFFLINE']!='1' or env['UV_PYTHON_DOWNLOADS']!='never':raise RuntimeError('offline_original_uv_required')
 script='set -eu; SCRIPT_DIR="$1"; ENV=dev; COMMAND=local; set -a; source "$SCRIPT_DIR/.env.dev"; set +a; export SERAPH_WORKSPACE_LIFECYCLE_PATH="$SCRIPT_DIR/docker-data/$ENV/workspace-lifecycle"; LOCAL_DEFAULT_MODEL="${LOCAL_DEFAULT_MODEL:-${DEFAULT_MODEL:-openrouter/anthropic/claude-sonnet-4}}"; SCREEN_CAPTURE_ARCHIVE_DIR="${SCREEN_CAPTURE_ARCHIVE_DIR:-$LOCAL_WORKSPACE_DIR/artifacts/screen-captures}"; SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR="${SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR:-$SCREEN_CAPTURE_ARCHIVE_DIR}"; SERAPH_DAEMON_STATUS_FILE="${SERAPH_DAEMON_STATUS_FILE:-$LOCAL_WORKSPACE_DIR/daemon-status.json}"; REPORT_ARCHIVE_DIR="${REPORT_ARCHIVE_DIR:-$LOCAL_WORKSPACE_DIR/artifacts/reports}"; export SCREEN_CAPTURE_ARCHIVE_DIR SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR SERAPH_DAEMON_STATUS_FILE REPORT_ARCHIVE_DIR; '
 if args.role=='schema_guard':script+='exec python3 -B "$SCRIPT_DIR/scripts/operator_schema_guard.py" "$LOCAL_WORKSPACE_DIR/seraph.db"'
 elif args.role=='workspace_helper':
  command=role['argv'][3]
  if role['argv'][:3]!=['python3','-B','-c']:raise RuntimeError('exact_workspace_command_required')
  script+='export PYTHONPATH="$SCRIPT_DIR/backend${PYTHONPATH:+:$PYTHONPATH}"; exec python3 -B -c "$2" "$LOCAL_WORKSPACE_DIR"'
 else:script+='cd "$SCRIPT_DIR/backend"; export WORKSPACE_DIR="$LOCAL_WORKSPACE_DIR" LLM_LOG_DIR="$LOCAL_LLM_LOG_DIR" UV_CACHE_DIR="$LOCAL_UV_CACHE_DIR" DEFAULT_MODEL="$LOCAL_DEFAULT_MODEL" SERAPH_LOCAL_SERVICE=backend; exec uv run python -B -m uvicorn src.app:create_app --factory --host 127.0.0.1 --port "$LOCAL_BACKEND_PORT"'
 target=Path(context['journey_directory'])/('negative-startup-'+args.role+'.json')
 if target.exists() or target.is_symlink():raise RuntimeError('fresh_observation_required')
 compatpath=BASE/'pidfd_compat.py';read(compatpath,PIDFD_COMPAT_SHA);spec=importlib.util.spec_from_file_location('_negative_pidfd',compatpath);compat=importlib.util.module_from_spec(spec);spec.loader.exec_module(compat);pidfd_capability=compat.install();pidfd_operation=getattr(os,'pidfd_open',compat.pidfd_open)
 argv=['/bin/bash','-c',script,'original-negative-'+args.role,str(source)]
 if args.role=='workspace_helper':argv.append(command)
 process=None;owned_start=None;pidfd=None
 try:
  process=subprocess.Popen(argv,cwd=role['cwd'],env=env,stdin=0,stdout=1,stderr=2,close_fds=True,umask=0o077)
  pidfd=pidfd_operation(process.pid,0)
  owned_start=start(process.pid)
  try:code=process.wait(timeout=20)
  except subprocess.TimeoutExpired:
   write(FIXTURE/('negative-'+args.role+'-outcome.json'),{'acceptance':False,'timeout':True,'preserve_fixture':True,'signals':0,'owned_root_pid':process.pid,'owned_root_start':owned_start});raise RuntimeError('owned_negative_probe_timeout_preserved')
  raw=read(target);record=json.loads(raw)
  if code!=78 or record['exit_code']!=78 or record['acceptance'] is not False or record['role']!=args.role or record['argv']!=role['argv'] or record['cwd']!=role['cwd'] or record['fds']!=role['fds'] or record['source_context_sha256']!=args.context_sha256:raise RuntimeError('exact_rejected_role_observation_required')
  if record['application_imported'] is not False or any(record[k] is not False for k in ('normal_receipt','ack','helper_main','uvicorn_main')):raise RuntimeError('negative_only_boundary_changed')
  if not (record['pid']==process.pid and record['start']==owned_start or record['parent_pid']==process.pid and record['parent_start']==owned_start):raise RuntimeError('actual_owned_child_identity_changed')
  if any(Path(context['receipt_dir']).iterdir()):raise RuntimeError('unexpected_normal_receipt_or_ack')
  write(FIXTURE/('negative-'+args.role+'-outcome.json'),{'acceptance':False,'role':args.role,'observed_exit_code':code,'owned_root_waited':True,'owned_root_pid':process.pid,'owned_root_start':owned_start,'record_sha256':hashlib.sha256(raw).hexdigest(),'source_context_sha256':args.context_sha256,'signals':0,'application_requests':0,'managed_acceptance':False,'preserve_fixture':True})
 except BaseException:
  failure=FIXTURE/('negative-'+args.role+'-custody-HOLD.json')
  write(failure,{'acceptance':False,'preserve_fixture':True,'unknown_capacity_HOLD':True,'pid':None if process is None else process.pid,'start':owned_start,'pidfd_captured':pidfd is not None,'root_wait_status':None if process is None else process.poll(),'closure_proven':False,'signals':0})
  raise
 finally:
  if pidfd is not None:os.close(pidfd)
if __name__=='__main__':main()
