import base64
import datetime
from functools import lru_cache

from app.media import Media
from app.utils import RequestUtils, ExceptionUtils
from config import Config


def get_login_wallpaper(time_now=None):
    """
    获取Base64编码的壁纸图片
    """
    if not time_now:
        time_now = datetime.datetime.now()
    wallpaper = Config().get_config('app').get('wallpaper')
    tmdbkey = Config().get_config('app').get('rmt_tmdbkey')
    if (not wallpaper or wallpaper == "themoviedb") and tmdbkey:
        # 每小时更新
        curr_time = datetime.datetime.strftime(time_now, '%Y%m%d%H')
        img_url, img_title, img_link = __get_themoviedb_wallpaper(curr_time)
    else:
        # 每天更新
        today = datetime.datetime.strftime(time_now, '%Y%m%d')
        img_url, img_title, img_link = __get_bing_wallpaper(today)
    img_enc = __get_image_b64(img_url)
    if img_enc:
        return img_enc, img_title, img_link
    return "", "", ""


@lru_cache(maxsize=1)
def __get_image_b64(img_url, cache_tag=None):
    """
    根据图片URL缓存
    如果遇到同一地址返回随机图片的情况, 需要视情况传递cache_tag参数
    """
    if img_url:
        res = RequestUtils().get_res(img_url)
        if res and res.status_code == 200:
            return base64.b64encode(res.content).decode()
    return ""


@lru_cache(maxsize=1)
def __get_themoviedb_wallpaper(cache_tag):
    """
    获取TheMovieDb的随机背景图
    cache_tag 缓存标记, 相同时会命中缓存
    """
    return Media().get_random_discover_backdrop()


@lru_cache(maxsize=1)
def __get_bing_wallpaper(today):
    """
    获取Bing每日壁纸
    """
    url = "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&today=%s" % today
    # Bing每日壁纸1080P高清接口
    try:
        resp = RequestUtils(timeout=5).get_res(url)
        if resp and resp.status_code == 200:
            try:
                json_data = resp.json()
                if json_data and json_data.get('images'):
                    for image in json_data.get('images'):
                        img_url = f"https://cn.bing.com{image.get('url')}" if 'url' in image else ''
                        img_title = image.get('title', '')
                        img_link = image.get('copyrightlink', '')
                        return img_url, img_title, img_link
            except Exception as json_err:
                ExceptionUtils.exception_traceback(json_err)
    except Exception as err:
        ExceptionUtils.exception_traceback(err)

    # 尝试备用图片源
    try:
        url = "https://bing.img.run/1920x1080.php"
        resp = RequestUtils(timeout=5).get_res(url)
        if resp and resp.status_code == 200:
            # 由于该 URL 直接返回图片，我们假设没有标题和链接
            img_url = url  # 使用请求的 URL 作为图片 URL
            img_title = "Bing Random Wallpaper"  # 默认标题
            img_link = "https://www.bing.com"  # 默认链接指向 Bing 主页
            return img_url, img_title, img_link
    except Exception as backup_err:
        ExceptionUtils.exception_traceback(backup_err)
    return '', '', ''
