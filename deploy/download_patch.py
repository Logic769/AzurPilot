"""只用标准库生成首次部署的下载补丁，不提前导入网络依赖。"""

import re


def patch_u2_downloads(source):
    """部署补丁与运行时调用同一下载器，不把请求头代码复制进第三方库。"""
    replacement = '''def cache_download(url, filename=None, timeout=None, storepath=None, logger=logger):
    # AzurPilot 统一下载请求头，避免默认 Origin 和无限等待。
    from deploy.downloads import cache_download as azurpilot_cache_download
    if not storepath:
        storepath = gen_cachepath(url)
    fallback_url = None
    if url.startswith('https://atx.azurpilot.com/'):
        fallback_url = GITHUB_BASEURL + '/atx-agent/releases/download/%s/%s' % (
            __atx_agent_version__, os.path.basename(url))
    return azurpilot_cache_download(url, storepath, timeout=timeout,
                                   fallback_url=fallback_url, logger=logger)


'''
    source = re.sub(r'(?ms)^def cache_download\([^\n]+\):\n.*?(?=^def |^class |\Z)', replacement, source, count=1)
    # 清除旧补丁的外层回退，避免新下载器已经回退后再次请求同一官方源。
    position = source.find('def mirror_download(')
    if position < 0:
        return source
    tail = re.sub(
        r"(?ms)^    if url.startswith\('https://atx.azurpilot.com/'\):\n.*?(?=^    github_host =)",
        "    if url.startswith('https://atx.azurpilot.com/'):\n"
        "        return cache_download(url, filename, storepath=storepath, logger=logger)\n\n",
        source[position:],
    )
    return source[:position] + tail
