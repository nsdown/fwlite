#!/usr/bin/env python
#-*- coding: UTF-8 -*-
#
# FGFW_Lite.py A Proxy Server help go around the Great Firewall
#
# Copyright (C) 2012-2013 Jiang Chao <sgzz.cj@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, see <http://www.gnu.org/licenses>.

from __future__ import print_function

__version__ = '0.3.3.0'

import sys
import os
from subprocess import Popen
import shlex
import time
import re
from threading import Thread, RLock, Timer
import atexit
import base64
import socket
import struct
import random
import tornado.ioloop
import tornado.iostream
import tornado.web
from tornado.httputil import HTTPHeaders
try:
    import configparser
except ImportError:
    import ConfigParser as configparser
configparser.RawConfigParser.OPTCRE = re.compile(r'(?P<option>[^=\s][^=]*)\s*(?P<vi>[=])\s*(?P<value>.*)$')
try:
    import ipaddress
    ip_address = ipaddress.ip_address
    ip_network = ipaddress.ip_network
except ImportError:
    import ipaddr
    ip_address = ipaddr.IPAddress
    ip_network = ipaddr.IPNetwork

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('FGFW-Lite')

WORKINGDIR = '/'.join(os.path.dirname(os.path.abspath(__file__).replace('\\', '/')).split('/')[:-1])
if ' ' in WORKINGDIR:
    print('no spacebar allowed in path')
    sys.exit()
os.chdir(WORKINGDIR)

if sys.platform.startswith('win'):
    PYTHON2 = '%s/include/Python27/python27.exe' % WORKINGDIR
else:
    PYTHON2 = '/usr/bin/env python2'

if not os.path.isfile('./userconf.ini'):
    with open('./userconf.ini', 'w') as f:
        f.write(open('./userconf.sample.ini').read())

if not os.path.isfile('./include/redirector.txt'):
    with open('./include/redirector.txt', 'w') as f:
        f.write('''\
|http://www.google.com/search forcehttps
|http://www.google.com/url forcehttps
|http://news.google.com forcehttps
|http://appengine.google.com forcehttps
|http://www.google.com.hk/url forcehttps
|http://www.google.com.hk/search forcehttps
/^http://www\.google\.com/?$/ forcehttps
|http://*.googlecode.com forcehttps
|http://*.wikipedia.org forcehttps
''')
if not os.path.isfile('./include/local.txt'):
    with open('./include/local.txt', 'w') as f:
        f.write('! local gfwlist config\n! rules: http://t.cn/zTeBinu\n')

for item in ['./include/redirector.txt', './userconf.ini', './include/local.txt']:
    with open(item) as f:
        data = open(item).read()
    with open(item, 'w') as f:
        f.write(data)

UPSTREAM_POOL = {}


class ProxyHandler(tornado.web.RequestHandler):
    SUPPORTED_METHODS = ['GET', 'POST', 'HEAD', 'PUT', 'DELETE', 'TRACE', 'CONNECT']

    def getparent(self, uri, host):
        self.ppname, pp = fgfwproxy.parentproxy(uri, host)
        self.pptype, self.pphost, self.ppport, self.ppusername,\
            self.pppassword = pp

    def prepare(self):
        uri = self.request.uri
        if '//' not in uri:
            uri = 'https://{}'.format(uri)
        host = self.request.host.split(':')[0]
        # redirector
        new_url = REDIRECTOR.get(uri, host)
        if new_url:
            if new_url.startswith('403'):
                self.send_error(status_code=403)
            else:
                self.redirect(new_url)
            return

        urisplit = uri.split('/')
        self.requestpath = '/'.join(urisplit[3:])

        if ':' in urisplit[2]:
            self.requestport = int(urisplit[2].split(':')[1])
        else:
            self.requestport = 443 if uri.startswith('https://') else 80
        self.getparent(uri, host)

        if self.pptype == 'socks5':
            self.upstream_name = '{}-{}-{}'.format(self.ppname, self.request.host, str(self.requestport))
        else:
            self.upstream_name = self.ppname if self.pphost else self.request.host

        logger.info('{} {} via {}'.format(self.request.method, self.request.uri.split('?')[0], self.ppname))

    @tornado.web.asynchronous
    def get(self):

        client = self.request.connection.stream

        def _get_upstream():

            def socks5_handshake(data=None):
                def get_server_auth_method(data=None):
                    self.upstream.read_bytes(2, socks5_auth)

                def socks5_auth(data=None):
                    if data == b'\x05\00':  # no auth needed
                        conn_upstream()
                    elif data == b'\x05\02':  # basic auth
                        self.upstream.write(b''.join([b"\x01",
                                            chr(len(self.ppusername)).encode(),
                                            self.ppusername.encode(),
                                            chr(len(self.pppassword)).encode(),
                                            self.pppassword.encode()]))
                        self.upstream.read_bytes(2, socks5_auth_finish)
                    else:  # bad day, something is wrong
                        fail()

                def socks5_auth_finish(data=None):
                    if data == b'\x01\x00':  # auth passed
                        conn_upstream()
                    else:
                        fail()

                def conn_upstream(data=None):
                    req = b''.join([b"\x05\x01\x00\x03",
                                   chr(len(self.request.host)).encode(),
                                   self.request.host.encode(),
                                   struct.pack(">H", self.requestport)])
                    self.upstream.write(req, post_conn_upstream)

                def post_conn_upstream(data=None):
                    self.upstream.read_bytes(4, read_upstream_data)

                def read_upstream_data(data=None):
                    if data.startswith(b'\x05\x00\x00\x01'):
                        self.upstream.read_bytes(6, conn)
                    elif data.startswith(b'\x05\x00\x00\x03'):
                        self.upstream.read_bytes(3, readhost)
                    else:
                        fail()

                def readhost(data=None):
                    self.upstream.read_bytes(data[0], conn)

                def conn(data=None):
                    _sent_request()

                def fail():
                    client.write(b'HTTP/1.1 500 socks5 proxy Connection Failed.\r\n\r\n')
                    self.upstream.close()
                    client.close()

                if self.ppusername:
                    authmethod = b"\x05\x02\x00\x02"
                else:
                    authmethod = b"\x05\x01\x00"
                self.upstream.write(authmethod, get_server_auth_method)

            def _create_upstream():
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
                self.upstream = tornado.iostream.IOStream(s)
                if self.pptype == 'http':
                    self.upstream.connect((self.pphost, int(self.ppport)), _sent_request)
                elif self.pptype == 'https':
                    self.upstream = tornado.iostream.SSLIOStream(s)
                    self.upstream.connect((self.pphost, int(self.ppport)), _sent_request)
                elif self.pptype is None:
                    self.upstream.connect((self.request.host.split(':')[0], self.requestport), _sent_request)
                elif self.pptype == 'socks5':
                    self.upstream.connect((self.pphost, int(self.ppport)), socks5_handshake)
                else:
                    client.write(b'HTTP/1.1 501 %s proxy not supported.\r\n\r\n' % self.pptype)
                    client.close()

            lst = UPSTREAM_POOL.get(self.upstream_name)
            self.upstream = None
            if isinstance(lst, list):
                for item in lst:
                    lst.remove(item)
                    if not item.closed():
                        self.upstream = item
                        break
            if self.upstream is None:
                _create_upstream()
            else:
                _sent_request()

        def read_from_upstream(data):
            if not client.closed():
                client.write(data)

        def _sent_request():
            if self.pptype == 'http' or self.pptype == 'https':
                s = '%s %s %s\r\n' % (self.request.method, self.request.uri, self.request.version)
                if self.ppusername and 'Proxy-Authorization' not in self.request.headers:
                    a = '%s:%s' % (self.ppusername, self.pppassword)
                    self.request.headers['Proxy-Authorization'] = 'Basic %s\r\n' % base64.b64encode(a.encode())
            else:
                s = '%s /%s %s\r\n' % (self.request.method, self.requestpath, self.request.version)
            for key, value in self.request.headers.items():
                s += '%s: %s\r\n' % (key, value)
            s += '\r\n'
            s = s.encode()
            if self.request.body:
                s += self.request.body + b'\r\n\r\n'
            _on_connect(s)

        def _on_connect(data=None):
            self.upstream.read_until_regex(b"\r?\n\r?\n", _on_headers)
            self.upstream.write(data)

        def _on_headers(data=None):
            read_from_upstream(data)
            self._headers_written = True
            data = data.decode()
            first_line, _, header_data = data.partition("\n")
            status_code = int(first_line.split()[1])
            headers = HTTPHeaders.parse(header_data)
            self._close_flag = True if headers.get('Connection') == 'close' else False

            if "Content-Length" in headers:
                if "," in headers["Content-Length"]:
                    # Proxies sometimes cause Content-Length headers to get
                    # duplicated.  If all the values are identical then we can
                    # use them but if they differ it's an error.
                    pieces = re.split(r',\s*', headers["Content-Length"])
                    if any(i != pieces[0] for i in pieces):
                        raise ValueError("Multiple unequal Content-Lengths: %r" %
                                         headers["Content-Length"])
                    headers["Content-Length"] = pieces[0]
                content_length = int(headers["Content-Length"])
            else:
                content_length = None

            if self.request.method == "HEAD" or status_code == 304:
                _finish()
            elif headers.get("Transfer-Encoding") == "chunked":
                self.upstream.read_until(b"\r\n", _on_chunk_lenth)
            elif content_length is not None:
                self.upstream.read_bytes(content_length, _finish, streaming_callback=read_from_upstream)
            elif headers.get("Connection") == "close":
                self.upstream.read_until_close(_finish)
            else:
                _finish()

        def _on_chunk_lenth(data):
            read_from_upstream(data)
            length = int(data.strip(), 16)
            self.upstream.read_bytes(length + 2,  # chunk ends with \r\n
                                     _on_chunk_data)

        def _on_chunk_data(data):
            read_from_upstream(data)
            if len(data) != 2:
                self.upstream.read_until(b"\r\n", _on_chunk_lenth)
            else:
                _finish()

        def _finish(data=None):
            if self.upstream_name not in UPSTREAM_POOL:
                UPSTREAM_POOL[self.upstream_name] = []
            lst = UPSTREAM_POOL.get(self.upstream_name)
            for item in lst:
                if item.closed():
                    lst.remove(item)
            if not self.upstream.closed():
                if self._close_flag:
                    self.upstream.close()
                else:
                    lst.append(self.upstream)
            self.finish(data)

        _get_upstream()

    @tornado.web.asynchronous
    def post(self):
        return self.get()

    @tornado.web.asynchronous
    def delete(self):
        return self.get()

    @tornado.web.asynchronous
    def trace(self):
        return self.get()

    @tornado.web.asynchronous
    def put(self):
        return self.get()

    @tornado.web.asynchronous
    def head(self):
        return self.get()

    @tornado.web.asynchronous
    def connect(self):
        client = self.request.connection.stream

        def read_from_client(data):
            if not upstream.closed():
                upstream.write(data)

        def read_from_upstream(data):
            if not client.closed():
                client.write(data)

        def client_close(data=None):
            if not upstream.closed():
                if data:
                    upstream.write(data)
                upstream.close()

        def upstream_close(data=None):
            if not client.closed():
                if data:
                    client.write(data)
                client.close()

        def start_tunnel(data=None):
            client.read_until_close(client_close, read_from_client)
            upstream.read_until_close(upstream_close, read_from_upstream)
            if data:
                read_from_client(data)

        def start_ssltunnel(data=None):
            client.read_until_close(client_close, read_from_client)
            upstream.read_until_close(upstream_close, read_from_upstream)
            read_from_upstream(b'HTTP/1.1 200 Connection established\r\n\r\n')

        def http_conntgt(data=None):
            if self.pptype == 'http' or self.pptype == 'https':
                s = '%s %s %s\r\n' % (self.request.method, self.request.uri, self.request.version)
                if 'Proxy-Authorization' not in self.request.headers and self.ppusername:
                    a = '%s:%s' % (self.ppusername, self.pppassword)
                    self.request.headers['Proxy-Authorization'] = 'Basic %s\r\n' % base64.b64encode(a.encode())
            else:
                s = '%s /%s %s\r\n' % (self.request.method, self.requestpath, self.request.version)
            if self.request.method != 'CONNECT':
                self.request.headers['Connection'] = 'close'
            for key, value in self.request.headers.items():
                s += '%s: %s\r\n' % (key, value)
            s += '\r\n'
            s = s.encode()
            if self.request.body:
                s += self.request.body + b'\r\n\r\n'
            start_tunnel(s)

        def socks5_handshake(data=None):
            def get_server_auth_method(data=None):
                upstream.read_bytes(2, socks5_auth)

            def socks5_auth(data=None):
                if data == b'\x05\00':  # no auth needed
                    conn_upstream()
                elif data == b'\x05\02':  # basic auth
                    upstream.write(b''.join([b"\x01",
                                   chr(len(self.ppusername)).encode(),
                                   self.ppusername.encode(),
                                   chr(len(self.pppassword)).encode(),
                                   self.pppassword.encode()]))
                    upstream.read_bytes(2, socks5_auth_finish)
                else:  # bad day, something is wrong
                    fail()

            def socks5_auth_finish(data=None):
                if data == b'\x01\x00':  # auth passed
                    conn_upstream()
                else:
                    fail()

            def conn_upstream(data=None):
                req = b''.join([b"\x05\x01\x00\x03",
                               chr(len(self.request.host)).encode(),
                               self.request.host.encode(),
                               struct.pack(">H", self.requestport)])
                upstream.write(req, post_conn_upstream)

            def post_conn_upstream(data=None):
                upstream.read_bytes(4, read_upstream_data)

            def read_upstream_data(data=None):
                if data.startswith(b'\x05\x00\x00\x01'):
                    upstream.read_bytes(6, conn)
                elif data.startswith(b'\x05\x00\x00\x03'):
                    upstream.read_bytes(3, readhost)
                else:
                    fail()

            def readhost(data=None):
                upstream.read_bytes(data[0], conn)

            def conn(data=None):
                if self.request.method == 'CONNECT':
                    start_ssltunnel()
                else:
                    http_conntgt()

            def fail():
                client.write(b'HTTP/1.1 500 socks5 proxy Connection Failed.\r\n\r\n')
                upstream.close()
                client.close()

            if self.ppusername:
                authmethod = b"\x05\x02\x00\x02"
            else:
                authmethod = b"\x05\x01\x00"
            upstream.write(authmethod, get_server_auth_method)

        if self.pphost is None:
            if self.request.method == 'CONNECT':
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
                upstream = tornado.iostream.IOStream(s)
                upstream.connect((self.request.host.split(':')[0], self.requestport), start_ssltunnel)
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
                upstream = tornado.iostream.IOStream(s)
                upstream.connect((self.request.host.split(':')[0], self.requestport), http_conntgt)
        elif self.pptype == 'http':
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            upstream = tornado.iostream.IOStream(s)
            upstream.connect((self.pphost, int(self.ppport)), http_conntgt)
        elif self.pptype == 'https':
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            upstream = tornado.iostream.SSLIOStream(s)
            upstream.connect((self.pphost, int(self.ppport)), http_conntgt)
        elif self.pptype == 'socks5':
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            upstream = tornado.iostream.IOStream(s)
            upstream.connect((self.pphost, int(self.ppport)), socks5_handshake)
        else:
            client.write(b'HTTP/1.1 501 %s proxy not supported.\r\n\r\n' % self.pptype)
            client.close()


class ForceProxyHandler(ProxyHandler):
    def getparent(self, uri, host):
        self.ppname, pp = fgfwproxy.parentproxy(uri, host, forceproxy=True)
        self.pptype, self.pphost, self.ppport, self.ppusername,\
            self.pppassword = pp


class autoproxy_rule(object):
    """docstring for autoproxy_rule
        (type,pattern)
        type:
            int
            DOMAIN = 0
            URI = 1
            KEYWORD = 2
            OVERRIDE_DOMAIN = 3
            OVERRIDE_URI =4
            OVERRIDE_KEYWORD = 5

        pattern:
            list
            [string, string, .....]
    """
    DOMAIN = 0
    URI = 1
    KEYWORD = 2
    REGEX = 3
    OVERRIDE_DOMAIN = 4
    OVERRIDE_URI = 5
    OVERRIDE_KEYWORD = 6
    OVERRIDE_REGEX = 7

    def __init__(self, arg):
        super(autoproxy_rule, self).__init__()
        if not isinstance(arg, str):
            if isinstance(arg, bytes):
                arg = arg.decode()
            else:
                raise TypeError("invalid type: must be a string(or bytes)")
        self.rule = arg.strip()
        if self.rule == '' or len(self.rule) < 3 or self.rule.startswith('!') or self.rule.startswith('['):
            raise ValueError("invalid autoproxy_rule: %s" % self.rule)
        self._type, self._ptrnlst = self._autopxy_rule_parse(self.rule)
        self.override = True if self._type >= self.OVERRIDE_DOMAIN else False

    def _autopxy_rule_parse(self, rule):
        def parse(rule):
            if rule.startswith('||'):
                return (self.DOMAIN, rule.replace('||', '').replace('/', '').split('*'))
            elif rule.startswith('|'):
                return (self.URI, rule.replace('|', '').split('*'))
            elif rule.startswith('/') and rule.endswith('/'):
                return (self.REGEX, [re.compile(rule[1:-1]), ])
            else:
                return (self.KEYWORD, rule.split('*'))

        if rule.startswith('@@'):
            a, b = parse(rule.replace('@@', ''))
            return (a+self.OVERRIDE_DOMAIN, b)
        else:
            return parse(rule)

    def match(self, url, domain=None):
        # url must be something like https://www.google.com
        ptrnlst = self._ptrnlst[:]

        def _match_domain():
            if domain.endswith(ptrnlst.pop()):
                if ptrnlst:
                    return _match_keyword(uri=domain)
                return True
            return False

        def _match_uri():
            s = ptrnlst.pop(0)
            if url.startswith(s):
                if ptrnlst:
                    return _match_keyword(index=len(s))
                return True
            return False

        def _match_keyword(uri=url, index=0):
            i = index
            while ptrnlst:
                s = ptrnlst.pop(0)
                if s in url:
                    i = uri.find(s, i) + len(s)
                else:
                    return False
            return True

        def _match_regex(uri=url, index=0):
            if ptrnlst[0].match(uri):
                return True
            return False

        if domain is None:
            domain = url.split('/')[2].split(':')[0]

        if self._type is self.DOMAIN:
            return _match_domain()
        elif self._type is self.URI:
            if url.startswith('https://'):
                if self.rule.startswith('|https://'):
                    return _match_uri()
                return False
            return _match_uri()
        elif self._type is self.KEYWORD:
            if url.startswith('https://'):
                return False
            return _match_keyword()
        elif self._type is self.REGEX:
            return _match_regex()

        elif self._type is self.OVERRIDE_DOMAIN:
            return _match_domain()
        elif self._type is self.OVERRIDE_URI:
            return _match_uri()
        elif self._type is self.OVERRIDE_KEYWORD:
            return _match_keyword()
        elif self._type is self.OVERRIDE_REGEX:
            return _match_regex()


class redirector(object):
    """docstring for redirector"""
    def __init__(self, arg=None):
        super(redirector, self).__init__()
        self.arg = arg
        self.config()

    def get(self, uri, host=None):
        for rule, result in self.list:
            if rule.match(uri, host):
                logger.info('Match redirect rule {}, {}'.format(rule.rule, result))
                if result == 'forcehttps':
                    return uri.replace('http://', 'https://', 1)
                return result
        return False

    def config(self):
        self.list = []

        with open('./include/redirector.txt') as f:
            for line in f:
                line = line.strip()
                if len(line.split()) == 2:  # |http://www.google.com/url forcehttps
                    try:
                        o = autoproxy_rule(line.split()[0])
                        if o.override:
                            raise Exception
                    except Exception:
                        pass
                    else:
                        self.list.append((o, line.split()[1]))

REDIRECTOR = redirector()


def run_proxy(port, start_ioloop=True):
    """
    Run proxy on the specified port. If start_ioloop is True (default),
    the tornado IOLoop will be started immediately.
    """
    print("Starting HTTP proxy on port {} and {}".format(port, str(int(port)+1)))
    app = tornado.web.Application([(r'.*', ProxyHandler), ])
    app.listen(8118)
    app2 = tornado.web.Application([(r'.*', ForceProxyHandler), ])
    app2.listen(8119)
    ioloop = tornado.ioloop.IOLoop.instance()
    if start_ioloop:
        ioloop.start()


def updateNbackup():
    while True:
        time.sleep(90)
        chkproxy()
        ifupdate()
        if conf.userconf.dgetbool('AutoBackupConf', 'enable', False):
            ifbackup()


def chkproxy():
    dit = conf.parentdict.copy()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    for k, v in dit.items():
        if v[1] is None:
            continue
        try:
            s.connect((v[1], v[2]))
        except Exception:
            del dit[k]
        else:
            s.close()
    conf.parentdictalive = dit


def ifupdate():
    if conf.userconf.dgetbool('FGFW_Lite', 'autoupdate'):
        lastupdate = conf.presets.dgetfloat('Update', 'LastUpdate', 0)
        if time.time() - lastupdate > conf.UPDATE_INTV * 60 * 60:
            update(auto=True)


def ifbackup():
    lastbackup = conf.userconf.dgetfloat('AutoBackupConf', 'LastBackup', 0)
    if time.time() - lastbackup > conf.BACKUP_INTV * 60 * 60:
        Thread(target=backup).start()


def update(auto=False):
    if auto:
        open("./include/dummy", 'w').close()
    conf.presets.set('Update', 'LastUpdate', str(time.time()))
    for item in FGFWProxyAbs.ITEMS:
        if item.enableupdate:
            item.update()
    Timer(4, restart).start()


def restart():
    conf.confsave()
    REDIRECTOR.config()
    for item in FGFWProxyAbs.ITEMS:
        item.config()
        item.restart()


def backup():
    import tarfile
    with conf.iolock:
        conf.userconf.set('AutoBackupConf', 'LastBackup', str(time.time()))
        conf.confsave()
    try:
        backuplist = conf.userconf.items('AutoBackup', raw=True)
        backupPath = conf.userconf.get('AutoBackupConf', 'BackupPath', raw=True)
    except:
        logger.error("read userconf.ini failed!")
    else:
        if not os.path.isdir(backupPath):
            try:
                os.makedirs(backupPath)
            except:
                logger.error('create dir %s failed!' % backupPath)
        if len(backuplist) > 0:
            logger.info("start packing")
            for i in range(len(backuplist)):
                if os.path.exists(backuplist[i][1]):
                    filepath = '%s/%s-%s.tar.bz2' % (backupPath, backuplist[i][0], time.strftime('%Y%m%d%H%M%S'))
                    logger.info('packing %s to %s' % (backuplist[i][1], filepath))
                    pack = tarfile.open(filepath, "w:bz2")
                    try:
                        pack.add(backuplist[i][1])
                    except Exception:
                        pack.close()
                        os.remove(filepath)
                        logger.info('Packing %s failed.' % filepath)
                    else:
                        pack.close()
                        logger.info('Done.')
        #remove old backup file
        rotation = conf.userconf.dgetint('AutoBackupConf', 'rotation', 10)
        filelist = os.listdir(str(backupPath))
        filelist.sort()
        surname = ''
        group = []
        for filename in filelist:
            if re.search(r'\d{14}\.tar\.bz2$', filename):
                if filename.split('-')[0] == surname:
                    group.append(filename)
                    if len(group) > rotation:
                        os.remove('%s/%s' % (backupPath, group.pop(0)))
                else:
                    group = []
                    group.append(filename)
                    surname = filename.split('-')[0]


class FGFWProxyAbs(object):
    """docstring for FGFWProxyAbs"""
    ITEMS = []

    def __init__(self):
        FGFWProxyAbs.ITEMS.append(self)
        self.subpobj = None
        self.config()
        self.daemon = Thread(target=self.start)
        self.daemon.daemon = True
        self.daemon.start()

    def config(self):
        self._config()

    def _config(self):
        self.cmd = ''
        self.cwd = ''
        self.filelist = []
        self.enable = True
        self.enableupdate = True

    def start(self):
        while True:
            if self.enable:
                if self.cwd:
                    os.chdir(self.cwd)
                self.subpobj = Popen(shlex.split(self.cmd))
                os.chdir(WORKINGDIR)
                self.subpobj.wait()
            time.sleep(3)

    def restart(self):
        try:
            self.subpobj.terminate()
        except Exception:
            pass

    def _update(self):
        self._listfileupdate()

    def update(self):
        if self.enable and self.enableupdate:
            self._update()

    def _listfileupdate(self):
        if len(self.filelist) > 0:
            for i in range(len(self.filelist)):
                url, path = self.filelist[i]
                etag = conf.presets.dget('Update', path.replace('./', '').replace('/', '-'), '')
                self.updateViaHTTP(url, etag, path)

    def updateViaHTTP(self, url, etag, path):
        import requests

        proxy = {'http': 'http://127.0.0.1:8118',
                 }
        header = {'If-None-Match': etag,
                  }
        cafile = './goagent/cacert.pem'
        try:
            r = requests.get(url, proxies=proxy, headers=header, timeout=5, verify=cafile)
        except Exception as e:
            logger.info('{} NOT updated. Reason: {}'.format(path, repr(e)))
        else:
            if r.status_code == 200:
                with open(path, 'wb') as localfile:
                    localfile.write(r.content)
                with conf.iolock:
                    conf.presets.set('Update', path.replace('./', '').replace('/', '-'), str(r.headers.get('etag')))
                with consoleLock:
                    logger.info('%s Updated.' % path)
            else:
                logger.info('{} NOT updated. Reason: {}'.format(path, str(r.status_code)))


class goagentabs(FGFWProxyAbs):
    """docstring for ClassName"""
    def __init__(self):
        FGFWProxyAbs.__init__(self)

    def _config(self):
        self.filelist = [['https://goagent.googlecode.com/git-history/3.0/local/proxy.py', './goagent/proxy.py'],
                         ['https://goagent.googlecode.com/git-history/3.0/local/proxy.ini', './goagent/proxy.ini'],
                         ['https://goagent.googlecode.com/git-history/3.0/local/cacert.pem', './goagent/cacert.pem'],
                         ]
        self.cwd = '%s/goagent' % WORKINGDIR
        self.cmd = '{} {}/goagent/proxy.py'.format(PYTHON2, WORKINGDIR)
        self.enable = conf.userconf.dgetbool('goagent', 'enable', True)
        self.enableupdate = conf.userconf.dgetbool('goagent', 'update', True)

        listen = conf.userconf.dget('goagent', 'listen', '127.0.0.1:8087')
        if ':' in listen:
            listen_ip, listen_port = listen.split(':')
        else:
            listen_ip = '127.0.0.1'
            listen_port = listen

        proxy = SConfigParser()
        proxy.read('./goagent/proxy.ini')
        proxy.set('listen', 'ip', listen_ip)
        proxy.set('listen', 'port', listen_port)

        if self.enable:
            conf.addparentproxy('goagnet', ('http', '127.0.0.1', int(listen_port), None, None))

        proxy.set('gae', 'profile', conf.userconf.dget('goagent', 'profile', 'google_cn'))
        proxy.set('gae', 'appid', conf.userconf.dget('goagent', 'goagentGAEAppid', 'mzu5gx1heh2beebo2'))
        proxy.set("gae", "password", conf.userconf.dget('goagent', 'goagentGAEpassword', ''))
        proxy.set('gae', 'obfuscate', conf.userconf.dget('goagent', 'obfuscate', '0'))
        proxy.set('gae', 'validate', conf.userconf.dget('goagent', 'validate', '0'))
        proxy.set("google_hk", "hosts", conf.userconf.dget('goagent', 'gaehkhosts', 'www.google.com|mail.google.com|www.l.google.com|mail.l.google.com|www.google.com.hk'))
        proxy.set('pac', 'enable', '0')
        proxy.set('paas', 'fetchserver', conf.userconf.dget('goagent', 'paasfetchserver', ''))
        if conf.userconf.dget('goagent', 'paasfetchserver'):
            proxy.set('paas', 'enable', '1')
            if self.enable:
                conf.addparentproxy('goagnet-paas', ('http', '127.0.0.1', 8088, None, None))

        if os.path.isfile("./include/dummy"):
            proxy.set('listen', 'visible', '0')
            os.remove("./include/dummy")
        else:
            proxy.set('listen', 'visible', '1')

        with open('./goagent/proxy.ini', 'w') as configfile:
            proxy.write(configfile)

        if not os.path.isfile('./goagent/CA.crt'):
            self.createCA()

    def createCA(self):
        '''
        ripped from goagent 2.1.14
        '''
        import OpenSSL
        ca_vendor = 'FGFW_Lite'
        keyfile = './goagent/CA.crt'
        key = OpenSSL.crypto.PKey()
        key.generate_key(OpenSSL.crypto.TYPE_RSA, 2048)
        ca = OpenSSL.crypto.X509()
        ca.set_serial_number(0)
        ca.set_version(2)
        subj = ca.get_subject()
        subj.countryName = 'CN'
        subj.stateOrProvinceName = 'Internet'
        subj.localityName = 'Cernet'
        subj.organizationName = ca_vendor
        subj.organizationalUnitName = '%s Root' % ca_vendor
        subj.commonName = '%s Root CA' % ca_vendor
        ca.gmtime_adj_notBefore(0)
        ca.gmtime_adj_notAfter(24 * 60 * 60 * 3652)
        ca.set_issuer(ca.get_subject())
        ca.set_pubkey(key)
        ca.add_extensions([
            OpenSSL.crypto.X509Extension(b'basicConstraints', True, b'CA:TRUE'),
            OpenSSL.crypto.X509Extension(b'keyUsage', False, b'keyCertSign, cRLSign'),
            OpenSSL.crypto.X509Extension(b'subjectKeyIdentifier', False, b'hash', subject=ca), ])
        ca.sign(key, 'sha1')
        with open(keyfile, 'wb') as fp:
            fp.write(OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, ca))
            fp.write(OpenSSL.crypto.dump_privatekey(OpenSSL.crypto.FILETYPE_PEM, key))
        import shutil
        if os.path.isdir('./goagent/certs'):
            shutil.rmtree('./goagent/certs')
        self.import_ca()

    def import_ca(self):
        '''
        ripped from goagent 3.0.0
        '''
        certfile = os.path.abspath('./goagent/CA.crt')
        dirname, basename = os.path.split(certfile)
        commonname = 'FGFW_Lite CA'
        if sys.platform.startswith('win'):
            with open(certfile, 'rb') as fp:
                certdata = fp.read()
                if certdata.startswith(b'-----'):
                    begin = b'-----BEGIN CERTIFICATE-----'
                    end = b'-----END CERTIFICATE-----'
                    certdata = base64.b64decode(b''.join(certdata[certdata.find(begin)+len(begin):certdata.find(end)].strip().splitlines()))
                import ctypes
                crypt32_handle = ctypes.windll.kernel32.LoadLibraryW('crypt32.dll')
                crypt32 = ctypes.WinDLL(None, handle=crypt32_handle)
                store_handle = crypt32.CertOpenStore(10, 0, 0, 0x4000 | 0x10000, 'ROOT')
                if not store_handle:
                    return -1
                ret = crypt32.CertAddEncodedCertificateToStore(store_handle, 0x1, certdata, len(certdata), 4, None)
                crypt32.CertCloseStore(store_handle, 0)
                del crypt32
                ctypes.windll.kernel32.FreeLibrary(crypt32_handle)
                return 0 if ret else -1
        elif sys.platform == 'darwin':
            return os.system('security find-certificate -a -c "%s" | grep "%s" >/dev/null || security add-trusted-cert -d -r trustRoot -k "/Library/Keychains/System.keychain" "%s"' % (commonname, commonname, certfile))
        elif sys.platform.startswith('linux'):
            import platform
            platform_distname = platform.dist()[0]
            if platform_distname == 'Ubuntu':
                pemfile = "/etc/ssl/certs/%s.pem" % commonname
                new_certfile = "/usr/local/share/ca-certificates/%s.crt" % commonname
                if not os.path.exists(pemfile):
                    return os.system('cp "%s" "%s" && update-ca-certificates' % (certfile, new_certfile))
            elif any(os.path.isfile('%s/certutil' % x) for x in os.environ['PATH'].split(os.pathsep)):
                return os.system('certutil -L -d sql:$HOME/.pki/nssdb | grep "%s" || certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "%s" -i "%s"' % (commonname, commonname, certfile))
            else:
                logging.warning('please install *libnss3-tools* package to import GoAgent root ca')
        return 0


class shadowsocksabs(FGFWProxyAbs):
    """docstring for ClassName"""
    def __init__(self):
        FGFWProxyAbs.__init__(self)

    def _config(self):
        self.filelist = [['https://github.com/clowwindy/shadowsocks/raw/master/shadowsocks/local.py', './shadowsocks/local.py'],
                         ['https://github.com/clowwindy/shadowsocks/raw/master/shadowsocks/encrypt.py', './shadowsocks/encrypt.py'],
                         ['https://github.com/clowwindy/shadowsocks/raw/master/shadowsocks/utils.py', './shadowsocks/utils.py']]
        self.cmd = '{} -B {}/shadowsocks/local.py'.format(PYTHON2, WORKINGDIR)
        self.cwd = '%s/shadowsocks' % WORKINGDIR
        if sys.platform.startswith('win'):
            self.cmd = 'c:/python27/python.exe -B %s/shadowsocks/local.py' % WORKINGDIR
            lst = ['./shadowsocks/shadowsocks-local.exe',
                   './shadowsocks/shadowsocks.exe']
            for f in lst:
                if os.path.isfile(f):
                    self.cmd = ''.join([WORKINGDIR, f[1:]])
                    break
        self.enable = conf.userconf.dgetbool('shadowsocks', 'enable', False)
        if self.enable:
            conf.addparentproxy('shadowsocks', ('socks5', '127.0.0.1', 1080, None, None))
        self.enableupdate = conf.userconf.dgetbool('shadowsocks', 'update', False)
        if not self.cmd.endswith('shadowsocks.exe'):
            server = conf.userconf.dget('shadowsocks', 'server', '')
            server_port = conf.userconf.dget('shadowsocks', 'server_port', '')
            password = conf.userconf.dget('shadowsocks', 'password', 'barfoo!')
            method = conf.userconf.dget('shadowsocks', 'method', 'table')
            self.cmd += ' -s {} -p {} -l 1080 -k {} -m {}'.format(server, server_port, password, method.strip('"'))


class cow_abs(FGFWProxyAbs):
    """docstring for cow_abs"""
    def __init__(self):
        FGFWProxyAbs.__init__(self)

    def _config(self):
        self.filelist = []
        self.cwd = '%s/cow' % WORKINGDIR
        if sys.platform.startswith('win'):
            self.cmd = '%s/cow/cow.exe' % WORKINGDIR
        else:
            self.cmd = '%s/cow/cow' % WORKINGDIR

        self.enable = conf.userconf.dgetbool('cow', 'enable', False)
        self.enableupdate = False
        configfile = []
        configfile.append('listen = %s' % conf.userconf.dget('cow', 'listen', '127.0.0.1:8118'))
        for key, item in conf.parentdict.items():
            pptype, pphost, ppport, ppusername, pppassword = item
            if key == 'direct':
                continue
            if pptype == 'http':
                configfile.append('httpParent = %s:%s' % (pphost, ppport))
            if pptype == 'socks5':
                configfile.append('socksParent = %s:%s' % (pphost, ppport))
        filepath = './cow/rc.txt' if sys.platform.startswith('win') else ''.join([os.path.expanduser('~'), '/.cow/rc'])
        with open(filepath, 'w') as f:
            f.write('\n'.join(configfile))


class fgfwproxy(FGFWProxyAbs):
    """docstring for ClassName"""
    def __init__(self, arg=''):
        FGFWProxyAbs.__init__(self)
        self.arg = arg

    def _config(self):
        self.filelist = [['https://autoproxy-gfwlist.googlecode.com/svn/trunk/gfwlist.txt', './include/gfwlist.txt'],
                         ['http://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest', './include/delegated-apnic-latest'],
                         # ['https://fgfw-lite.googlecode.com/git/include/FGFW_Lite.py', './include/FGFW_Lite.py'],
                         # ['https://fgfw-lite.googlecode.com/git/include/cloud.txt', './include/cloud.txt'],
                         # ['https://fgfw-lite.googlecode.com/git/userconf.sample.ini', './userconf.sample.ini'],
                         ['https://github.com/v3aqb/fgfw-lite/raw/master/include/FGFW_Lite.py', './include/FGFW_Lite.py'],
                         ['https://github.com/v3aqb/fgfw-lite/raw/master/include/cloud.txt', './include/cloud.txt'],
                         ['https://github.com/v3aqb/fgfw-lite/raw/master/userconf.sample.ini', './userconf.sample.ini'],
                         ]
        self.enable = conf.userconf.dgetbool('fgfwproxy', 'enable', True)
        self.enableupdate = conf.userconf.dgetbool('fgfwproxy', 'update', True)
        self.listen = conf.userconf.dget('fgfwproxy', 'listen', '8118')
        if self.enable:
            self.chinaroute()
            self.conf()

    def start(self):
        if self.enable:
            if ':' in self.listen:
                run_proxy(self.listen.split(':')[1], address=self.listen.split(':')[0])
            else:
                run_proxy(self.listen)

    @classmethod
    def conf(cls):
        conf.parentdict = {}
        conf.addparentproxy('direct', (None, None, None, None, None))

        cls.gfwlist = []
        cls.gfwlist_force = []

        def add_rule(line, force=False):
            try:
                o = autoproxy_rule(line)
            except Exception:
                pass
            else:
                if force is True:
                    cls.gfwlist_force.append(o)
                else:
                    cls.gfwlist.append(o)

        with open('./include/local.txt') as f:
            for line in f:
                add_rule(line, force=True)

        with open('./include/cloud.txt') as f:
            for line in f:
                add_rule(line, force=True)

        with open('./include/gfwlist.txt') as f:
            for line in base64.b64decode(f.read()).split():
                add_rule(line)

    @classmethod
    def parentproxy(cls, uri, domain=None, forceproxy=False):
        '''
            decide which parentproxy to use.
            url:  'https://www.google.com'
            domain: 'www.google.com'
        '''

        if uri and domain is None:
            domain = uri.split('/')[2].split(':')[0]

        cls.inchinadict = {}

        def ifgfwlist_force():
            for rule in cls.gfwlist_force:
                if rule.match(uri, domain):
                    logger.info('Autoproxy Rule match {}'.format(rule.rule))
                    return not rule.override
            return False

        def ifhost_in_china():
            if domain is None:
                return False
            result = cls.inchinadict.get('domain')
            if result is None:
                try:
                    ipo = ip_address(socket.gethostbyname(domain))
                except Exception:
                    return False
                result = False
                for net in cls.chinanet:
                    if ipo in net:
                        result = True
                        break
                cls.inchinadict[domain] = result
            return result

        def ifgfwlist():
            for rule in cls.gfwlist:
                if rule.match(uri, domain):
                    logger.info('Autoproxy Rule match {}'.format(rule.rule))
                    return not rule.override
            return False

        # select parent via uri
        parentlist = list(conf.parentdictalive.keys())
        if ifgfwlist_force():
            parentlist.remove('direct')
            if uri.startswith('ftp://'):
                if 'goagent' in parentlist:
                    parentlist.remove('goagent')
            if parentlist:
                ppname = random.choice(parentlist)
                return (ppname, conf.parentdictalive.get(ppname))
        if ifhost_in_china():
            return ('direct', conf.parentdictalive.get('direct'))
        if forceproxy or ifgfwlist():
            parentlist.remove('direct')
            if uri.startswith('ftp://'):
                if 'goagent' in parentlist:
                    parentlist.remove('goagent')
            if parentlist:
                ppname = random.choice(parentlist)
                return (ppname, conf.parentdictalive.get(ppname))
        return ('direct', conf.parentdictalive.get('direct'))

    @classmethod
    def chinaroute(cls):

        cls.chinanet = []
        cls.chinanet.append(ip_network('192.168.0.0/16'))
        cls.chinanet.append(ip_network('172.16.0.0/12'))
        cls.chinanet.append(ip_network('10.0.0.0/8'))
        cls.chinanet.append(ip_network('127.0.0.0/8'))
        # ripped from https://github.com/fivesheep/chnroutes
        import math
        with open('./include/delegated-apnic-latest') as remotefile:
            data = remotefile.read()

        cnregex = re.compile(r'apnic\|cn\|ipv4\|[0-9\.]+\|[0-9]+\|[0-9]+\|a.*', re.IGNORECASE)
        cndata = cnregex.findall(data)

        for item in cndata:
            unit_items = item.split('|')
            starting_ip = unit_items[3]
            num_ip = int(unit_items[4])

            #mask in *nix format
            mask2 = 32 - int(math.log(num_ip, 2))

            cls.chinanet.append(ip_network('{}/{}'.format(starting_ip, mask2)))


class SConfigParser(configparser.ConfigParser):
    """docstring for SSafeConfigParser"""
    def __init__(self):
        configparser.ConfigParser.__init__(self)

    def dget(self, section, option, default=None):
        if default is None:
            default = ''
        value = self.get(section, option)
        if not value:
            value = default
        return value

    def dgetfloat(self, section, option, default=0):
        try:
            value = self.getfloat(section, option)
        except Exception:
            value = float(default)
        return value

    def dgetint(self, section, option, default=0):
        try:
            value = self.getint(section, option)
        except Exception:
            value = int(default)
        return value

    def dgetbool(self, section, option, default=False):
        try:
            value = self.getboolean(section, option)
        except Exception:
            value = bool(default)
        return value

    def get(self, section, option, raw=False, vars=None):
        try:
            value = configparser.ConfigParser.get(self, section, option, raw, vars)
            if value is None:
                raise Exception
        except Exception:
            value = ''
        return value

    def set(self, section, option, value):
        if not self.has_section(section):
            self.add_section(section)
        configparser.ConfigParser.set(self, section, option, value)


class Config(object):
    def __init__(self):
        self.iolock = RLock()
        self.presets = SConfigParser()
        self.userconf = SConfigParser()
        self.reload()
        self.UPDATE_INTV = 6
        self.BACKUP_INTV = 24
        self.parentdict = {}

    def reload(self):
        self.presets.read('presets.ini')
        self.userconf.read('userconf.ini')

    def confsave(self):
        self.presets.write(open('presets.ini', 'w'))
        self.userconf.write(open('userconf.ini', 'w'))

    def addparentproxy(self, name, proxy):
        '''
        {
            'direct': (None, None, None, None, None),
            'goagent': ('http', '127.0.0.1', 8087, None, None)
        }  # type, host, port, username, password
        '''
        self.parentdict[name] = proxy

conf = Config()
consoleLock = RLock()


@atexit.register
def atexit_do():
    for item in FGFWProxyAbs.ITEMS:
        item.enable = False
        item.restart()
    conf.confsave()


def main():
    if conf.userconf.dgetbool('fgfwproxy', 'enable', True):
        fgfwproxy()
    if conf.userconf.dgetbool('goagent', 'enable', True):
        goagentabs()
    if conf.userconf.dgetbool('shadowsocks', 'enable', False):
        shadowsocksabs()
    if conf.userconf.dgetbool('https', 'enable', False):
        host = conf.userconf.dget('https', 'host', '')
        port = conf.userconf.dget('https', 'port', '443')
        user = conf.userconf.dget('https', 'user', None)
        passwd = conf.userconf.dget('https', 'passwd', None)
        conf.addparentproxy('https', ('https', host, int(port), user, passwd))
    if conf.userconf.dgetbool('cow', 'enable', False):
        cow_abs()
    conf.parentdictalive = conf.parentdict.copy()
    updatedaemon = Thread(target=updateNbackup)
    updatedaemon.daemon = True
    updatedaemon.start()
    while True:
        try:
            exec(raw_input().strip())
        except Exception as e:
            print(repr(e))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
