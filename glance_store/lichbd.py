'''
@author: frank
'''
import errno
import subprocess
import time

class ShellError(Exception):
    '''shell error'''
    
class ShellCmd(object):
    '''
    classdocs
    '''
    
    def __init__(self, cmd, workdir=None, pipe=True):
        '''
        Constructor
        '''
        self.cmd = cmd
        if pipe:
            self.process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=subprocess.PIPE, executable='/bin/sh', cwd=workdir)
        else:
            self.process = subprocess.Popen(cmd, shell=True, executable='/bin/sh', cwd=workdir)
            
        self.stdout = None
        self.stderr = None
        self.return_code = None
        
    def __call__(self, is_exception=True):
        (self.stdout, self.stderr) = self.process.communicate()
        if is_exception and self.process.returncode != 0:
            err = []
            err.append('failed to execute shell command: %s' % self.cmd)
            err.append('return code: %s' % self.process.returncode)
            err.append('stdout: %s' % self.stdout)
            err.append('stderr: %s' % self.stderr)
            raise ShellError('\n'.join(err))
            
        self.return_code = self.process.returncode
        return self.stdout

def call(cmd, exception=True, workdir=None):
    return ShellCmd(cmd, workdir)(exception)

def __call_shellcmd(cmd, exception=False, workdir=None):
    shellcmd =  ShellCmd(cmd, workdir)
    shellcmd(exception)
    return shellcmd

def call_try(cmd, exception=False, workdir=None, try_num = None):
    if try_num is None:
        try_num = 10

    shellcmd = None
    for i in range(try_num):
        shellcmd = __call_shellcmd(cmd, False, workdir)
        if shellcmd.return_code == 0:
            break

        time.sleep(1)

    return shellcmd
    #return shellcmd.stdout, shellcmd.stderr, shellcmd.return_code

def raise_exp(shellcmd):
    err = []
    err.append('failed to execute shell command: %s' % shellcmd.cmd)
    err.append('return code: %s' % shellcmd.process.returncode)
    err.append('stdout: %s' % shellcmd.stdout)
    err.append('stderr: %s' % shellcmd.stderr)
    raise ShellError('\n'.join(err))

def call_timeout():
    pass

def lichbd_mkdir(path):
    shellcmd = call_try('/opt/mds/lich/libexec/lich --mkdir %s' % (path))
    if shellcmd.return_code != 0:
        if shellcmd.return_code == errno.EEXIST:
            pass
        else:
            raise_exp(shellcmd)

def lichbd_create_raw(path, size):
    shellcmd = call_try('qemu-img create -f raw %s %s' % (path, size))
    if shellcmd.return_code != 0:
        if shellcmd.return_code == errno.EEXIST:
            pass
        else:
            raise_exp(shellcmd)

def lichbd_copy(src_path, dst_path):
    shellcmd = None
    for i in range(5):
        shellcmd = call_try('/opt/mds/lich/libexec/lich --copy %s %s' % (src_path, dst_path))
        if shellcmd.return_code == 0:
            return shellcmd
        else:
            if dst_path.startswith(":"):
                call("rm -rf %s" % (dst_path.lstrip(":")))
            else:
                lichbd_unlink(dst_path)

    raise_exp(shellcmd)

def lichbd_unlink(path):
    shellcmd = call_try('/opt/mds/lich/libexec/lich --unlink %s' % path)
    if shellcmd.return_code != 0:
        if shellcmd.return_code == errno.ENOENT:
            pass
        else:
            raise_exp(shellcmd)

def lichbd_file_size(path):
    shellcmd = call_try("/opt/mds/lich/libexec/lich --stat %s|grep Size|awk '{print $2}'" % (path))
    if shellcmd.return_code != 0:
        raise_exp(shellcmd)

    size = shellcmd.stdout.strip()
    return long(size)

def lichbd_cluster_stat():
    shellcmd = call_try('lich.cluster --stat')
    if shellcmd.return_code != 0:
        raise_exp(shellcmd)

    return shellcmd.stdout
