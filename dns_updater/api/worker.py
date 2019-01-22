# -*- coding: utf-8 -*-

import commands
import os
import re
import socket
import shutil
import threading
from hashlib import md5

from oslo.config import cfg

from dns_updater.utils.updater_util import DnsdbApi
from dns_updater.utils.updater_util import send_alarm_email
from dns_updater.utils.updater_util import backup_file

from dnsdb_common.library.exception import UpdaterErr
from dnsdb_common.library.log import getLogger
log = getLogger(__name__)

CONF = cfg.CONF


def _is_local_dns(group_name=None):
    if group_name is None:
        group_name = CONF.host_group
    return group_name.lower().startswith('local')


def _get_named_dir():
    return CONF.bind_conf.named_dir

def _get_acl_dir():
    return CONF.bind_conf.acl_dir

def _get_local_hostname():
    return socket.gethostname()

def _get_named_path():
    named_dir = _get_named_dir()
    return os.path.join(named_dir, 'named.conf')

def can_reload(group_name):
    return DnsdbApi.can_reload(group_name)['data']

def update_host_md5(named_conf_md5):
    try:
        DnsdbApi.update_host_md5(CONF.host_ip, named_conf_md5)
    except Exception as e:
        send_alarm_email(u'主机%s更新named.conf文件成功，更新数据库失败\n原因%s' % (_get_local_hostname(), e))
        log.exception(e)
    return


def get_named_md5():
    name_file = _get_named_path()
    with open(name_file) as f:
        content = f.read()
    if _is_local_dns():
        res = re.findall('listen-on {[\s\d\.;]+};', content)[0]
        content = content.replace(res, '#localdns_listen_mark')
    return md5(content).hexdigest()


# 使用named-checkconf检查需要reload的配置文件
def check_named_conf(named_file):
    if CONF.etc.env == 'dev':
        return
    status, output = commands.getstatusoutput('%s %s' % (CONF.bind_conf.named_checkconf, named_file))
    if status == 0:
        log.info('check %s ok' % named_file)
    else:
        raise UpdaterErr('check %s fail, %s' % (named_file, output))


def copy_named_conf(named_file):
    named_path = _get_named_path()
    # 备份
    backup_file('named', named_path)
    status, output = commands.getstatusoutput(
        'cp %s %s && chown named:named %s' % (named_file, named_path, named_path))
    if status == 0:
        log.info('update name.conf ok')
    else:
        raise UpdaterErr('copy_named_conf failed: %s' % output)


# reload配置文件使之生效
def reload_conf():
    if CONF.etc.env == 'dev':
        return
    status, output = commands.getstatusoutput('%s reload' % CONF.bind_conf.rndc)
    if status == 0:
        log.info('rndc reload success')
    else:
        raise UpdaterErr('reload named.conf failed: %s' % output)


def update_named_conf(group_name):
    named_conf = DnsdbApi.get_named_conf(group_name)['data']

    named_dir = _get_named_dir()
    new_name_path = os.path.join(named_dir, group_name)
    to_use_file = '{0}_used'.format(new_name_path)
    with open(new_name_path, 'w') as f:
        f.write(named_conf)
    shutil.copy(new_name_path, to_use_file)
    # 如果是local dns  检查前先获取本机ip 将listen-on {ip};添加到option中
    if _is_local_dns():
        status, output = commands.getstatusoutput(
            "ip address | grep inet | awk '{print $2}' | awk -F '/' '{print $1}' | grep  -E '(^127\.|^192\.|^10\.)'")
        iplist = [ip.strip() for ip in output.split('\n')]
        if len(iplist) <= 1:
            raise UpdaterErr('listen ip %s replace failed' % ','.join(iplist))
        log.info('listen ip: %s' % iplist)
        with open(to_use_file) as f:
            content = f.read()
        content = content.replace('#localdns_listen_mark', 'listen-on {%s;};' % (';'.join(iplist)))
        open(to_use_file, 'w').write(content)

    check_named_conf(to_use_file)
    if can_reload(group_name):
        copy_named_conf(to_use_file)
        reload_conf()


class UpdateConfThread(threading.Thread):
    def __init__(self, update_type, kwargs):
        super(UpdateConfThread, self).__init__()
        self.update_type = update_type
        self.group_name = kwargs['group_name']
        self.kwargs = kwargs


    def update_named(self):
        named_conf_md5 = get_named_md5()
        if named_conf_md5 == self.kwargs['group_conf_md5']:
            return update_host_md5(named_conf_md5)
        update_named_conf(self.group_name)
        return update_host_md5(self.kwargs['group_conf_md5'])


    def update_acl(self):
        acl_dir = _get_acl_dir()
        acl_files = self.kwargs.get('acl_files', [])
        filenames = {filename: os.path.join(acl_dir, filename) for filename in acl_files}

        for acl_file, acl_path in filenames.iteritems():
            # 生成新的配置文件
            content = DnsdbApi.get_acl_content(acl_file)['data']
            with open('{}.tmp'.format(acl_path), 'w') as f:
                f.write(content)


        # 重新加载配置
        if can_reload(self.group_name):
            tmp_conf_dict = {}
            for acl_file in filenames.values():
                # 备份原来配置文件
                backup_file('acl', acl_file)
                back = acl_file + '.bak'
                shutil.copy(acl_file, back)
                # 拷贝新的配置文件
                shutil.copy('{}.tmp'.format(acl_file), acl_file)
                tmp_conf_dict[acl_file] = back

            # 检查文件语法
            try:
                check_named_conf(_get_named_path())
            except UpdaterErr as e:
                # 配置文件还原
                for conf_file, back in tmp_conf_dict.iteritems():
                    shutil.copy(back, conf_file)
                raise
            reload_conf()


    def run(self):
        msg = ''
        is_success = True
        try:
            if self.update_type == 'named.conf':
                self.update_named()
            elif self.update_type == 'acl':
                self.update_acl()

        except Exception as e:
            send_alarm_email(u'更新文件失败\n主机: %s\n原因: %s' % (_get_local_hostname(), e))
            log.exception(e)
            msg = str(e)
            is_success = False

        deploy_id = self.kwargs.get('deploy_id', None)
        if deploy_id:
            DnsdbApi.update_deploy_info(deploy_id, is_success, msg)


def start_update_thread(update_type, **kwargs):
    thread = UpdateConfThread(update_type, kwargs)
    thread.start()