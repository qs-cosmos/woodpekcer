# coding: utf-8

""" 基于 IPv4 检测网络延迟和丢包率

基本思路:
    ICMP, TCP, UDP, HTTP 均可用于检测网络延迟和丢包率, 但是每种协议的检测过程和
检测结果,都有比较多的差异, 因此根据检测过程中采用的协议划分为多个类。同时这些类
之间又存在很多相似性,因此构建一个基类包含所有的相同操作,其他类共同继承于该基类。
    与大多数平台实现的 ping 不同的是, DNS 解析过程将单独作为一个重要的检测内容。
因此这里的输入是IP 地址而不是Domain name。IPv4/IPv6 将在初始化时检测，从而创建合
适的网络通信套接字, 进行后续的检测。

"""

import re
import threading
import time
import timeit

from config.constant import PROTO, SOCKET
from config.logger import Logger
from core.spider.structure import ICMPingStruct
from core.packet.icmp import ICMP
from core.packet.ip import IPV4


class ICMPing(object):
    """ 采用ICMP协议检测网络延迟和丢包率

    基本思路:
    - 仅提供 Once ping one time. 的方法, ping 多次交由上层实现;
    - 记录最近 n 次 ping 的结果 和 单独记录最新一次 ping 的结果;
    - 用 config 方法重置 dst 时, 则会清空上述记录, 即使前后 dst 一致;
    - ping 方法内部不处理异常 socket.error, 打印日志后直接抛出-交由上层处理。

    """
    def __init__(self):
        """ 初始化 ICMPing

        @param dst: 目的主机IP
        @type  dst: ipv4/ipv6
        """
        self.logger = Logger.get()
        self.dst = None
        self.sock = None
        self.timeout = 1
        self.interval = 0.0
        self.id = 0
        self.records = []

    def config(self, dst=None, interval=0.0, timeout=1, rst=False):
        """ 配置 ICMPing

        @param dst: 目的主机IP
        @type  dst: ipv4/ipv6

        @param interval: 发送 ICMP报文 的间隔时间(单位: s)
        @type  interval: double

        @param timeout: 超时时间(单位: s)
        @type  timeout: double

        @param count: 发送 ICMP报文 的总数
        @type  count: int

        @param rst: 清空 record/records
        @type  rst: Bool
        """
        new = False
        if dst not in {None, ''}:
            self.dst = re.sub(r'\s+', '', dst)
            new = True

        if new or rst:
            # 重置 目的 ip 时, 重置record/records
            # 记录最近 n 次ping的结果
            self.records = []
            # 创建一个新的套接字 self.sock
            self.sock = SOCKET.close(self.sock)
            self.sock = SOCKET.create(self.dst, PROTO.ICMP)

        self.interval = interval
        self.timeout = timeout
        self.id = threading.currentThread().ident & 0xffff

    def __send(self, seq):
        """ 发送 ICMP 回送请求报文

        @return: (发送时间, ICMP 回送请求报文)
        @rtype : (double, ICMP())
        """
        try:
            # 构建 ICMP 报文
            sent_icmp = ICMP()
            sent_icmp.construct(ID=self.id, Seq=seq)
            # 发送 ICMP 报文
            self.sock.sendto(sent_icmp.icmp, (self.dst, 1))
            self.logger.info('Successfully send a echo request icmp packet.')
            return (timeit.default_timer(), sent_icmp)
        except Exception:
            self.logger.exception('Failed to send a echo request icmp packet.')
            return (-1, None)

    def __recv(self, sent_time):
        """ 接收 ICMP 回送响应报文

        基本思路:
        采用 select.select 监控 socket - Waiting for I/O completion
        目的 : 用来监控 Ping 是否超时
        - socket.recvfrom 会一直等待 I/O, 而没有 timeout 机制
        - 当 select.select 完成时
          - 未超时: 则通过socket.recvfrom获取数据, 检测是否为回送响应 ICMP 报文
          - 超时: 则终止循环, 抛出 Timeout Error
        设置 select.select() 剩余超时时间
        - 当 socket 中有数据到来时, 不一定是 回送响应 ICMP 报文
        - 因此, 需要进行多次 select.select()
        - 对于 send - recv 这一过程的总超时时间为 self.timeout 是确定的
        - 每进行一次循环, select.select() 的超时时间也就要相应的减少
        - 以保证 总的超时时间为 : self.timeout
        已知. 发送开始时间 则 select.select 剩余超时时间为 :
          self.time_out - timeit.default_timer() + self.sent_time

        @return: (recv_time, ICMP 回送响应报文, IP数据报)
        @rtype : (double, ICMP(), IP())
        """
        self.logger.info('Expecting a echo response icmp packet.')
        remain_time = 0
        while True:
            remain_time = self.timeout - timeit.default_timer() + sent_time
            recv_time, packet = SOCKET.recvfrom(self.sock, remain_time)
            if recv_time < 0:
                return (recv_time, None, None)

            ipv4 = IPV4()
            icmp = ICMP()
            ok = ipv4.analysis(packet)
            ok = ok and icmp.analysis(packet[ipv4.header_length:])
            if ok and icmp.id == self.id and ipv4.src == self.dst:
                info = 'Successfully get a echo response icmp packet.'
                self.logger.info(info)
                return (recv_time, icmp, ipv4)
            else:
                self.logger.warning('Get a unexpected packet.')

    def ping(self, seq=0):
        """ ping one time. """
        if self.sock is None:
            return False
        self.logger.info('Start to icmping the ip %s' % (self.dst))
        sent_time, sent_icmp = self.__send(seq)
        recv_time, recv_icmp, recv_ipv4 = self.__recv(sent_time)

        # 整理记录
        record = ICMPingStruct()
        record.seq = seq
        record.ttl = 0 if recv_ipv4 is None else recv_ipv4.ttl
        # 记录 ICMP 回送请求报文
        record.sent_size = 0 if sent_icmp is None else len(sent_icmp.icmp)
        record.sent_timestamp = sent_time * 1000
        # 记录 ICMP 回送响应报文
        record.recv_size = 0 if recv_icmp is None else len(recv_icmp.icmp)
        record.recv_timestamp = recv_time * 1000
        # 计算延迟
        latency = (recv_time - sent_time) * 1000
        record.latency = -1 if latency <= 0 else latency

        # 存入历史记录
        self.records.append(record)
        self.logger.info('End icmping the ip %s' % (self.dst))

        # 间隔 interval 时间, 再发起下一次请求
        try:
            self.logger.info('ICMPing: sleep for %f s' % self.interval)
            time.sleep(self.interval)
        except Exception:
            self.logger.exception('Failed to sleep.')
            return False
        return True

    def json(self):
        records = []
        try:
            records = map(lambda x: x.json(), self.records)
        except Exception:
            self.logger.info('The type of records is: %s' % type(self.records))
            self.logger.exception('Failed to transfer into json.')
        finally:
            return records
