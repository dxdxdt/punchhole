#!/bin/env python3
import atexit
import os
import select
import signal
import socket
import sys

nb_th = len(os.sched_getaffinity(0))
host = ''
port = 3477
af = socket.AF_INET6
debug = False
fifo_path = None

MAPPED_V4_PREFIX = '::ffff:'
MAX_CLIENT_TIME = 30 # seconds
UDP_SEND_FLAGS = (
	getattr(socket, 'MSG_DONTWAIT', 0) |
	getattr(socket, 'MSG_NOSIGNAL', 0)
)
FIFO_PREFIX = os.getenv('TSTUN_VAR_PREFIX') or 'tstun-fifo'
FIFO_BUFSIZE = 46 + 1 + 5 # INET6_ADDRSTRLEN(46) + ' '(1) + port(5)

def mkfifo_path (k: str) -> str:
	return '%s/%s' % ( k[0:2], k[2:64], )

def force_unlink (path):
	try: os.unlink(path)
	except FileNotFoundError: pass

def handle_exit_after_fifo (*_):
	global fifo_path
	force_unlink(fifo_path)
	sys.exit(1)

def do_fifo (k: str, msg: bytes) -> bytes:
	global fifo_path
	path = FIFO_PREFIX + '/' + mkfifo_path(k)
	dir = os.path.dirname(path)
	fd = -1

	def do_open (flags: int):
		nonlocal fd
		if fd >= 0: os.close(fd)
		fd = os.open(path, flags)
	def do_read ():
		nonlocal fd
		do_open(os.O_RDONLY)
		ret = os.read(fd, FIFO_BUFSIZE)
		return ret
	def do_write ():
		nonlocal fd, msg
		do_open(os.O_WRONLY)
		os.write(fd, msg)

	os.makedirs(dir, exist_ok = True)
	fifo_path = path
	signal.signal(signal.SIGALRM, handle_exit_after_fifo)
	try:
		os.mkfifo(path)
		# This will do no good when the process dies. Just restart the service
		# everyday.
		atexit.register(os.unlink, path)
		do_write()
		ret = do_read()
	except FileExistsError:
		ret = do_read()
		do_write()
	finally:
		if fd >= 0: os.close(fd)

	return ret

def mkmsg (sa: tuple) -> bytes:
	sa = list[str]([str(e) for e in sa[:2]])

	if sa[0].startswith(MAPPED_V4_PREFIX):
		sa[0] = sa[0][len(MAPPED_V4_PREFIX):]

	return (' '.join(sa)).encode('ascii')

def child_main (s: socket.socket, sa: tuple[str, int]) -> int:
	msg = s.recv(32)
	s.shutdown(socket.SHUT_RD)
	if len(msg) != 32:
		return 1
	key = msg.hex()

	msg = do_fifo(key, mkmsg(sa))
	s.send(msg)
	s.shutdown(socket.SHUT_WR)

	return 0

def server_loop (s: socket.socket):
	global debug
	poll = select.poll()
	poll.register(s, select.POLLIN)

	while True:
		try: poll.poll()
		# don't die upon random real-time signals
		except InterruptedError: continue
		try: c, sa = s.accept()
		except BlockingIOError: continue

		if debug:
			sys.stderr.write('accept(): ' + str(sa) + os.linesep)

		try:
			pid = os.fork()
			if pid == 0:
				if debug:
					sys.stderr.write('fork(): ' + str(os.getpid()) + os.linesep)

				signal.alarm(MAX_CLIENT_TIME)
				s.close()
				ec = child_main(c, sa)
				c.close()
				sys.exit(ec)
		except OSError as e:
			sys.stderr.write('fork(): ' + str(e) + os.linesep)
			# keep going
		c.close()

# Automatically reap child processes. Careful! This is a Linux thing.
signal.signal(signal.SIGCHLD, signal.SIG_IGN)

with socket.socket(af, socket.SOCK_STREAM, socket.IPPROTO_TCP) as s_tcp:
	s_tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	s_tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
	s_tcp.setblocking(False)

	s_tcp.bind((host, port))
	s_tcp.listen()

	server_loop(s_tcp)
