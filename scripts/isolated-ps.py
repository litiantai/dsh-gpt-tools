#!/usr/bin/env python3
"""隔离测试使用的非特权进程查询，仅支持平台实际使用的 ps 输出格式。

结构布局取自 macOS SDK sys/proc_info.h；不读取或输出进程环境变量。
"""
import ctypes as c
import os
import struct
import sys


class BSDInfo(c.Structure):
    _fields_=[(n,c.c_uint32) for n in ('flags','status','xstatus','pid','ppid','uid','gid','ruid','rgid','svuid','svgid','reserved')]+[
        ('comm',c.c_char*16),('name',c.c_char*32)]+[(n,c.c_uint32) for n in ('nfiles','pgid','jobc','tdev','tpgid')]+[
        ('nice',c.c_int32),('start_sec',c.c_uint64),('start_usec',c.c_uint64)]


def main():
    lib=c.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    lib.proc_pidinfo.argtypes=[c.c_int,c.c_int,c.c_uint64,c.c_void_p,c.c_int]
    args=sys.argv[1:]
    if '-p' in args:
        pid=int(args[args.index('-p')+1]);buffer=c.create_string_buffer(262144);size=c.c_size_t(len(buffer))
        mib=(c.c_int*3)(1,49,pid);system=c.CDLL(None,use_errno=True)
        if system.sysctl(mib,3,buffer,c.byref(size),None,0):
            sys.exit(1)
        data=buffer.raw[:size.value];count=struct.unpack('i',data[:4])[0]
        start=data.index(b'\0',4)+1
        while start<len(data) and data[start]==0:start+=1
        print(' '.join(part.decode(errors='replace') for part in data[start:].split(b'\0')[:count]))
        return
    if args not in (['-axo','pid=,ppid=,pgid=,stat=,lstart='],):
        raise SystemExit('隔离进程查询不支持此输出格式')
    length=lib.proc_listpids(1,0,None,0)
    pids=(c.c_int*(length//4+4096))()
    count=lib.proc_listpids(1,0,pids,c.sizeof(pids))//4
    for pid in pids[:count]:
        info=BSDInfo()
        if pid<=0 or lib.proc_pidinfo(pid,3,0,c.byref(info),c.sizeof(info))!=c.sizeof(info) or info.uid!=os.getuid():
            continue
        print(info.pid,info.ppid,info.pgid,'Z' if info.status==5 else 'S',f'{info.start_sec}.{info.start_usec}')


if __name__=='__main__':main()
