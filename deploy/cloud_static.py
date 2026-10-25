"""静态云文档的构建、串行发布、预览验证及失败回滚。"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from deploy.atomic import atomic_write
from deploy.cloud_api import response_document, service_url
from deploy.cloud_contract import NOTICE_PATH, UPDATE_PATH, notice_document, timestamp, update_document
from deploy.http import get

KINDS = {'notice': (NOTICE_PATH, notice_document), 'update': (UPDATE_PATH, update_document)}
TEMPLATES = Path(__file__).resolve().parent / 'cloudflare'


@contextmanager
def publication_lock(directory, timeout=30):
    """同一 Worker 跨进程串行执行，锁文件由操作系统释放而非删除。"""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.publish.lock').open('a+b') as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('等待该 Worker 的发布锁超时') from None
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_bundle(kind, document, directory, *, hostname='api.example.com', zone='example.com', name=None):
    """只生成指定 Worker 的完整静态目录，不改动 CDN 更新项目。"""
    path, validate = KINDS[kind]
    validate(document)
    directory = Path(directory)
    config = json.loads((TEMPLATES / kind / 'wrangler.jsonc').read_text(encoding='utf-8'))
    hostname = hostname.strip().lower()
    zone = zone.strip().lower()
    if '/' in hostname or ':' in hostname or not (hostname == zone or hostname.endswith('.' + zone)):
        raise ValueError('Worker 域名必须属于配置的 zone，不能包含协议或路径')
    config['routes'] = [{'pattern': hostname + path.rsplit('/', 1)[0] + '/*', 'zone_name': zone}]
    if name:
        config['name'] = name
    target = directory / 'public' / path.lstrip('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(str(target), json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    atomic_write(str(directory / 'public/_headers'), (TEMPLATES / kind / 'public/_headers').read_text(encoding='utf-8'))
    atomic_write(str(directory / 'wrangler.jsonc'), json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    return config


def verify_document(base, kind, expected):
    path, validate = KINDS[kind]
    response = get(service_url(base) + path, timeout=10, allow_redirects=False,
                   headers={'Cache-Control': 'no-cache'})
    try:
        actual = validate(response_document(response))
        if actual != expected:
            raise ValueError('静态地址尚未返回本次完整文档')
    finally:
        response.close()


class StaticPublisher:
    """要求 Worker 已初始化；上传和预览失败不会改变当前生产部署。"""

    def __init__(self, output, *, command=('wrangler',), runner=None, verifier=verify_document):
        self.output = Path(output)
        self.command = tuple(command)
        self.runner = runner or self._run
        self.verifier = verifier

    def _run(self, arguments):
        result = subprocess.run([*self.command, *arguments], check=True, capture_output=True,
                                text=True, encoding='utf-8', timeout=600,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return result.stdout

    @staticmethod
    def _versions(status):
        """异常或未知控制台输出立即停止，不猜测回滚目标。"""
        if not isinstance(status, dict) or not isinstance(status.get('versions'), list):
            raise ValueError('无法读取 Worker 当前部署版本，取消发布')
        versions = status['versions']
        if not versions or any(not isinstance(item.get('version_id'), str)
                               or type(item.get('percentage')) not in (int, float) for item in versions):
            raise ValueError('Worker 当前部署版本结构无效')
        if sum(item['percentage'] for item in versions) != 100:
            raise ValueError('Worker 当前部署流量比例无效')
        return [f'{item["version_id"]}@{item["percentage"]}' for item in versions]

    def publish(self, kind, document, *, hostname, zone, preview_base, name=None):
        path, validate = KINDS[kind]
        validate(document)
        if hostname == 'api.example.com' or hostname.endswith('.example.com'):
            raise ValueError('发布前必须配置实际 Worker 域名')
        preview_base = service_url(preview_base)
        directory = self.output / kind
        with publication_lock(directory):
            previous_document = directory / 'public' / path.lstrip('/')
            if previous_document.exists() and (directory / 'published.json').exists():
                previous = validate(json.loads(previous_document.read_text(encoding='utf-8')))
                if timestamp(previous['updated_at'], 'updated_at') > timestamp(document['updated_at'], 'updated_at'):
                    raise ValueError('旧文档不能覆盖较新的已发布文档')
                if kind == 'notice' and previous['announcement'] != document['announcement']:
                    if timestamp(previous['updated_at'], 'updated_at') == timestamp(document['updated_at'], 'updated_at'):
                        raise ValueError('公告内容改变时必须更新 updated_at')
                    if previous['announcement'] and document['announcement'] \
                            and previous['announcement']['id'] == document['announcement']['id']:
                        raise ValueError('公告内容改变时必须更新公告标识')
            with tempfile.TemporaryDirectory(prefix='cloud-stage-', dir=directory) as stage:
                config = build_bundle(kind, document, stage, hostname=hostname, zone=zone, name=name)
                config_path = str(Path(stage) / 'wrangler.jsonc')
                shared = ['--config', config_path]
                previous = self._versions(json.loads(self.runner(['deployments', 'status', '--json', *shared])))
                tag = kind + '-' + hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:24]
                self.runner(['versions', 'upload', '--tag', tag, '--preview-alias', 'cloud-publish',
                             '--keep-vars', *shared])
                versions = json.loads(self.runner(['versions', 'list', '--json', *shared]))
                version = next((item['id'] for item in versions
                                if item.get('annotations', {}).get('workers/tag') == tag), None)
                if not version:
                    raise ValueError('无法找到本次上传的 Worker 版本')
                self.verifier(preview_base, kind, document)
                saved_paths = [directory / relative for relative in (
                    'wrangler.jsonc', 'public/_headers', 'public/' + path.lstrip('/'), 'published.json')]
                saved = {target: target.read_bytes() if target.exists() else None for target in saved_paths}
                try:
                    self.runner(['versions', 'deploy', f'{version}@100', '--yes', *shared])
                    self.verifier('https://' + hostname, kind, document)
                    build_bundle(kind, document, directory, hostname=hostname, zone=zone, name=config['name'])
                    atomic_write(str(directory / 'published.json'), json.dumps(
                        {'version_id': version, 'updated_at': document['updated_at']}, ensure_ascii=False) + '\n')
                except Exception:
                    try:
                        self.runner(['versions', 'deploy', *previous, '--yes', *shared])
                    finally:
                        for target, content in saved.items():
                            if content is None:
                                target.unlink(missing_ok=True)
                            else:
                                atomic_write(str(target), content)
                    raise
                return version


def main():
    parser = argparse.ArgumentParser(description='生成或发布 Cloudflare 公告/策略静态文件')
    parser.add_argument('kind', choices=KINDS)
    parser.add_argument('document', type=Path)
    parser.add_argument('--output', type=Path, default=Path('dist/cloud-static'))
    parser.add_argument('--hostname', default='api.example.com')
    parser.add_argument('--zone', default='example.com')
    parser.add_argument('--name')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--preview-base', help='cloud-publish-<worker>.<账户子域>.workers.dev 的 HTTPS 基址')
    parser.add_argument('--wrangler', default='wrangler', help='已安装 Wrangler 的可执行文件')
    args = parser.parse_args()
    document = json.loads(args.document.read_text(encoding='utf-8'))
    if args.publish:
        if not args.preview_base:
            parser.error('--publish 必须指定 --preview-base')
        version = StaticPublisher(args.output, command=(args.wrangler,)).publish(
            args.kind, document, hostname=args.hostname, zone=args.zone,
            preview_base=args.preview_base, name=args.name)
        print(f'发布并验证完成：{version}')
    else:
        build_bundle(args.kind, document, args.output / args.kind,
                     hostname=args.hostname, zone=args.zone, name=args.name)
        print('静态目录已生成，未执行云端发布')


if __name__ == '__main__':
    main()
