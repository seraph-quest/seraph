"""External Root retained-FD owner for three rejected startup observations only."""
import argparse,fcntl,hashlib,importlib.util,json,os,stat,subprocess,time
from pathlib import Path
BASE=Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12')
OUT=Path(__file__).resolve().parent
COLLECTOR=OUT/'collect_rejected_role.py'
FIXTURE=BASE/'managed-execution'
PYTHON=Path('/home/pawel/repos/seraph/.agent-worktrees/986-a1-descriptor-projection/.agent-evidence/986/accepted-cache-r174/.venv/bin/python')
PYTHON_CANONICAL=Path('/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu/bin/python3.12')
PYTHON_SHA='0dc3a692fa85fcdb7f1a5877d2adf179809ac417a07ffde2373c832863800a15'
PINS={'/home/pawel/repos/seraph/.agent-evidence/986/c6/native-startup-negative-observation-r243/PINS.json': 'c4adf27feda849193bcaeed48e539a43d7e8a92008069b87fe5c15e919040fa2', '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/bootstrap/root_prepare.py': '5ebac196353eeff4befa978df23839a4d2b173c29610b38ca734329b7fd96923', '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/bootstrap/role_stdio.py': 'f53433085733780c93af370fa9d30bb115558c1d8c7e3430c33df33a7896f99c', '/home/pawel/repos/seraph/.agent-evidence/986/c6/native-startup-negative-observation-r243/produce_negative_contexts.py': '565900ddd7506f67065c67cc3507eee38a169a0d30380baea4bb4ae33a2d4eaf', '/home/pawel/repos/seraph/.agent-evidence/986/c6/native-startup-fd-owner-r246/collect_rejected_role.py': '57b34b83b557fcbe73d20b88c798582674841bf8d353a70557da0f179a841ee1', '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/negative_context_binding.py': 'abfddd557b61f193e20564aca85217ec22019336943645c7d4fd41ea6854159c', '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/HELPER-PINS.json': '3ff6d5bfe31ed69941995b48a595cf822ddd4c69197b1e511422dfe3d2c7f830', '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/pidfd_compat.py': 'e8b7160de15c1020e7dbbf97abf364c2bf6cf34c5f467b81208bab4bc459289e', '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/collect_rejected_role.py': '7ea24a1d623c3db687358489016f9ff5ef8869d477f0e9db26d0b0f67bef2549', '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/negative_context_binding.py': 'abfddd557b61f193e20564aca85217ec22019336943645c7d4fd41ea6854159c'}
ROLES=('schema_guard','workspace_helper','backend')
ROLE_CONTEXTS={'schema_guard': '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/schema_guard-context.json', 'workspace_helper': '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/workspace_helper-context.json', 'backend': '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/backend-context.json'}
BASE_CONTEXT_SHA=None
def digest(raw):return hashlib.sha256(raw).hexdigest()
def private_directory(path):
 info=path.lstat()
 if path.resolve(strict=True)!=path or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:raise RuntimeError('private_directory_changed')
def read(path,pin=None):
 path=Path(path);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:
  before=os.fstat(fd)
  if path.resolve(strict=True)!=path or not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o600:raise RuntimeError('private_source_input_changed')
  chunks=[]
  while True:
   raw=os.read(fd,1048576)
   if not raw:break
   chunks.append(raw)
   if sum(map(len,chunks))>16*1024*1024:raise RuntimeError('input_bound')
  raw=b''.join(chunks);after=os.fstat(fd)
  if len(raw)!=before.st_size or any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')):raise RuntimeError('input_changed_during_read')
  if pin is not None and digest(raw)!=pin:raise RuntimeError('source_hash_changed')
  return raw
 finally:os.close(fd)
def module(path,name):
 read(path,PINS[str(path)]);spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value
def record(path,value):
 raw=(json.dumps(value,sort_keys=True)+'\n').encode();fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
 with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
def held_append(path):
 private_directory(path.parent);before=path.lstat()
 if path.resolve(strict=True)!=path or not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o600:raise RuntimeError('original_private_log_required')
 fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW|os.O_CLOEXEC)
 after=os.fstat(fd)
 if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):os.close(fd);raise RuntimeError('log_identity_changed')
 return fd
def identity(fd,*,projected=False):
 info=os.fstat(fd);return {'device':info.st_dev,'inode':info.st_ino,'mode':info.st_mode,'uid':info.st_uid,'nlink':info.st_nlink,'open_flags':fcntl.fcntl(fd,fcntl.F_GETFL),'descriptor_flags':0 if projected else fcntl.fcntl(fd,fcntl.F_GETFD)}
def owner_remaining(started):
 remaining=180-(time.monotonic()-started)
 if remaining<=0:raise RuntimeError('total_owner_bound_HOLD')
 return remaining
def compact_context(path,value):
 private_directory(path.parent)
 raw=(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()
 if len(raw)>16*1024*1024:raise RuntimeError('input_bound')
 if json.loads(raw)!=value:raise RuntimeError('compact_context_not_lossless')
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
 with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
 directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
 try:os.fsync(directory)
 finally:os.close(directory)
 if read(path)!=raw:raise RuntimeError('exclusive_compact_context_changed')
 return {'sha256':digest(raw),'bytes':len(raw)}
def prepare_compact_contexts(context,base_sha,started):
 owner_remaining(started)
 original_fields=set(context)
 if len(original_fields)!=32 or 'negative_startup_role' in context or 'negative_copy_metadata' in context:raise RuntimeError('original_full_base_field_closure_required')
 cache=FIXTURE/'source/backend/.venv';metadata={}
 expected={path:digest for path,digest in context['source_file_pins'].items() if str(path).startswith(str(cache)+'/')}
 actual={str(path) for path in cache.rglob('*') if path.is_file() and not path.is_symlink()}
 if actual!=set(expected):raise RuntimeError('exact_copied_cache_frontier_required')
 for name,digest in expected.items():
  owner_remaining(started)
  path=Path(name);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
  try:
   before=os.fstat(fd)
   if path.resolve(strict=True)!=path or not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or stat.S_IMODE(before.st_mode) not in (0o664,0o775):raise RuntimeError('exact_copied_cache_modes_required')
   result=hashlib.sha256()
   while True:
    owner_remaining(started)
    chunk=os.read(fd,1048576)
    if not chunk:break
    result.update(chunk)
   after=os.fstat(fd)
   if result.hexdigest()!=digest or any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')):raise RuntimeError('copied_cache_changed')
   metadata[name]={'device':before.st_dev,'inode':before.st_ino,'mode':before.st_mode,'uid':before.st_uid,'nlink':before.st_nlink}
  finally:os.close(fd)
 if len(metadata)!=15118:raise RuntimeError('exact15118_cache_metadata_required')
 context['negative_copy_metadata']=metadata
 result={}
 for role in ROLES:
  owner_remaining(started)
  # One complete variant at a time; same full fields and source/cache digests.
  variant=dict(context);variant['negative_startup_role']=role
  env=dict(variant['launcher_env']);env['SERAPH_MANAGED_TEST_CONTEXT']=ROLE_CONTEXTS[role];variant['launcher_env']=env
  if set(variant)!=original_fields|{'negative_copy_metadata','negative_startup_role'} or len(variant)!=34 or len(variant['python_roles'])!=3 or any(r['startup_path'] is not None for r in variant['python_roles']):raise RuntimeError('full_NULL_role_context_required')
  result[role]=compact_context(Path(ROLE_CONTEXTS[role]),variant)
 # Every context is stable, complete and <=16MiB before ANY collector Popen.
 for role in ROLES:
  owner_remaining(started)
  raw=read(Path(ROLE_CONTEXTS[role]),result[role]['sha256'])
  if len(raw)!=result[role]['bytes'] or len(raw)>16*1024*1024 or len(json.loads(raw))!=34:raise RuntimeError('all_context_prelaunch_size_gate')
 record(OUT/'negative-context-readback.json',{'source_base_sha256':base_sha,'role_context_sha256':{r:v['sha256'] for r,v in result.items()},'role_context_bytes':{r:v['bytes'] for r,v in result.items()},'all_vectors_NULL':True,'acceptance':False,'metadata_rows':15118,'real_stdio_resealed_this_owner':True})
 return {r:v['sha256'] for r,v in result.items()}
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--prepared-context-sha256',required=True);args=parser.parse_args()
 handles=[];process=None;pidfd=None;owned_start=None;role_name=None;completed=[];started=time.monotonic()
 private_directory(OUT);private_directory(FIXTURE)
 if OUT!=Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12'):raise RuntimeError('exact_successor_control_root_required')
 marker=OUT/'owner-once.json';record(marker,{'acceptance':False,'roles':list(ROLES),'maximum_attempts':1})
 try:
  for path,pin in PINS.items():read(path,pin)
  if PYTHON.resolve(strict=True)!=PYTHON_CANONICAL or digest(PYTHON_CANONICAL.read_bytes())!=PYTHON_SHA:raise RuntimeError('pinned_cached_interpreter_changed')
  status=Path('/proc/self/status').read_text()
  if 'NoNewPrivs:\t1' not in status or 'Seccomp:\t2' not in status:raise RuntimeError('Root_inherited_R3_required')
  base_context_sha=args.prepared_context_sha256
  if len(base_context_sha)!=64 or any(c not in '0123456789abcdef' for c in base_context_sha):raise RuntimeError('exact_actual_prepared_base_required')
  initial=read(FIXTURE/'context.json',base_context_sha);context=json.loads(initial)
  if 'negative_startup_role' in context or len(context['python_roles'])!=3 or sorted(r['name'] for r in context['python_roles'])!=sorted(ROLES) or any(r['startup_path'] is not None for r in context['python_roles']):raise RuntimeError('fresh_all_NULL_base_required')
  collector_env=dict(os.environ)
  expected={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','TZ':'UTC','SERAPH_CORDIS_TEST_NODE':'/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64/bin/node','SERAPH_CORDIS_TEST_NODE22':'/home/pawel/.asdf/installs/nodejs/22.12.0/bin/node'}
  if collector_env!=expected:raise RuntimeError('exact_R3_controller_environment_required')
  controller=held_append(FIXTURE/'controller.log');handles.append(controller)
  backend=held_append(FIXTURE/'source/logs/seraph-local-backend.log');handles.append(backend)
  devnull=os.open('/dev/null',os.O_RDONLY|os.O_CLOEXEC);handles.append(devnull)
  if not stat.S_ISCHR(os.fstat(devnull).st_mode) or fcntl.fcntl(devnull,fcntl.F_GETFL)&os.O_ACCMODE!=os.O_RDONLY:raise RuntimeError('read_only_devnull_required')
  stdio=module(BASE/'bootstrap/role_stdio.py','_Root_exact_role_stdio')
  context=stdio.seal(context,controller_log_fd=controller,backend_log_fd=backend,devnull_fd=devnull)
  context_pins=prepare_compact_contexts(context,base_context_sha,started)
  if digest(read(FIXTURE/'context.json'))!=base_context_sha:raise RuntimeError('original_base_modified')
  helperpins=json.loads(read(BASE/'HELPER-PINS.json',PINS[str(BASE/'HELPER-PINS.json')]))
  compatibility=module(BASE/'pidfd_compat.py','_Root_owned_pidfd');pidfd_capability=compatibility.install();pidfd_open=getattr(os,'pidfd_open',compatibility.pidfd_open)
  # Original backend shell >> reopens without O_NOFOLLOW. Authenticate exact
  # private path and retained inode, then make that exact access-mode view;
  # this descriptor is never a new FD grant or pass_fds entry.
  path=FIXTURE/'source/logs/seraph-local-backend.log';private_directory(path.parent)
  info=path.lstat()
  if path.resolve(strict=True)!=path or (info.st_dev,info.st_ino)!=(os.fstat(backend).st_dev,os.fstat(backend).st_ino):raise RuntimeError('private_backend_reopen_identity_changed')
  backend_view=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CLOEXEC);handles.append(backend_view)
  if (os.fstat(backend_view).st_dev,os.fstat(backend_view).st_ino)!=(info.st_dev,info.st_ino):raise RuntimeError('private_backend_reopen_changed')
  for role_name in ROLES:
   if time.monotonic()-started>=180:raise RuntimeError('total_owner_bound_HOLD')
   for path,pin in PINS.items():read(path,pin)
   for path,pin in helperpins.items():read(path,pin)
   raw=read(Path(ROLE_CONTEXTS[role_name]));role_context=json.loads(raw);context_sha=digest(raw)
   if context_sha!=context_pins[role_name] or role_context['negative_startup_role']!=role_name:raise RuntimeError('actual_role_context_changed')
   validator=module(OUT/'negative_context_binding.py','_Root_exact_negative_validator')
   dotenv_path=BASE/'bootstrap/managed_fixture_dotenv.py';read(dotenv_path,helperpins[str(dotenv_path)]);spec=importlib.util.spec_from_file_location('_Root_exact_dotenv',dotenv_path);dotenv=importlib.util.module_from_spec(spec);spec.loader.exec_module(dotenv)
   validator.validate(role_context,role_context['launcher_env'],role_context['source_root'],dotenv.read_dotenv(role_context['dotenv']),digest(read(Path(role_context['dotenv']))))
   original=next(r for r in role_context['python_roles'] if r['name']==role_name)
   output=backend_view if role_name=='backend' else controller
   actual={'0':identity(devnull,projected=True),'1':identity(output,projected=True),'2':identity(output,projected=True)}
   if any(actual[k]!=original['fds'][k] for k in actual):raise RuntimeError('retained_role_stdio_projection_changed')
   argv=[str(PYTHON),'-I','-B',str(COLLECTOR),role_name,'--context-sha256',context_sha,'--helper-pins-sha256',PINS[str(BASE/'HELPER-PINS.json')]]
   process=None;pidfd=None;owned_start=None;role_started=time.monotonic()
   if min(20-(time.monotonic()-role_started),owner_remaining(started))<=0:raise RuntimeError('role_owner_bound_HOLD')
   process=subprocess.Popen(argv,cwd='/home/pawel/repos/seraph',env=collector_env,stdin=devnull,stdout=output,stderr=output,close_fds=True)
   pidfd=pidfd_open(process.pid,0)
   owned_start=int(Path(f'/proc/{process.pid}/stat').read_text().rsplit(')',1)[1].split()[19])
   remaining=min(20-(time.monotonic()-role_started),180-(time.monotonic()-started))
   if remaining<=0:raise RuntimeError('role_owner_bound_HOLD')
   code=process.wait(timeout=remaining)
   owner_remaining(started)
   if code!=0:raise RuntimeError('rejected_role_collector_failed')
   result=json.loads(read(FIXTURE/('negative-'+role_name+'-outcome.json')))
   if result['acceptance'] is not False or result['observed_exit_code']!=78 or result['owned_root_waited'] is not True or result['source_context_sha256']!=context_sha or result['signals']!=0 or result['managed_acceptance'] is not False:raise RuntimeError('exact_negative_outcome_required')
   completed.append({'role':role_name,'collector_waited':True,'collector_exit_code':code,'outcome_sha256':digest(read(FIXTURE/('negative-'+role_name+'-outcome.json'))),'context_sha256':context_sha})
   os.close(pidfd);pidfd=None;process=None;owned_start=None
  record(OUT/'owner-result.json',{'acceptance':False,'managed_acceptance':False,'completed':completed,'signals':0,'application_requests':0,'root_stdio_retained_through_all_probes':True,'preserve_fixture':True})
 except BaseException as error:
  record(OUT/'owner-HOLD.json',{'acceptance':False,'preserve_fixture':True,'unknown_capacity_HOLD':True,'closure_proven':False,'role':role_name,'pid':None if process is None else process.pid,'start':owned_start,'pidfd_captured':pidfd is not None,'collector_wait_status':None if process is None else process.poll(),'completed':completed,'signals':0,'exception_class':type(error).__name__})
  raise
 finally:
  if pidfd is not None:os.close(pidfd)
  for fd in reversed(handles):os.close(fd)
if __name__=='__main__':main()
