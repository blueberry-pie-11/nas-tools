import io
import os
import shutil
import sys
import requests
import pickle
from threading import Lock
from filelock import FileLock
import ruamel.yaml
import tempfile

# 种子名/文件名要素分隔字符
SPLIT_CHARS = r"\.|\s+|\(|\)|\[|]|-|\+|【|】|/|～|;|&|\||#|_|「|」|~"
# 默认User-Agent
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36"
# 收藏了的媒体的目录名，名字可以改，在Emby中点击红星则会自动将电影转移到此分类下，需要在Emby Webhook中配置用户行为通知
RMT_FAVTYPE = '精选'
# 支持的媒体文件后缀格式
RMT_MEDIAEXT = ['.mp4', '.mkv', '.ts', '.iso',
                '.rmvb', '.avi', '.mov', '.mpeg',
                '.mpg', '.wmv', '.3gp', '.asf',
                '.m4v', '.flv', '.m2ts', '.strm',
                '.tp']
# 支持的字幕文件后缀格式
RMT_SUBEXT = ['.srt', '.ass', '.ssa']
# 支持的音轨文件后缀格式
RMT_AUDIO_TRACK_EXT = ['.mka']
# 电视剧动漫的分类genre_ids
ANIME_GENREIDS = ['16']
# 默认过滤的文件大小，150M
RMT_MIN_FILESIZE = 150 * 1024 * 1024
# 删种检查时间间隔
AUTO_REMOVE_TORRENTS_INTERVAL = 1800
# 下载文件转移检查时间间隔，
PT_TRANSFER_INTERVAL = 300
# TMDB信息缓存定时保存时间
METAINFO_SAVE_INTERVAL = 600
# SYNC目录同步聚合转移时间
SYNC_TRANSFER_INTERVAL = 60
# RSS队列中处理时间间隔
RSS_CHECK_INTERVAL = 300
# 刷新订阅TMDB数据的时间间隔（小时）
RSS_REFRESH_TMDB_INTERVAL = 6
# 刷流删除的检查时间间隔
BRUSH_REMOVE_TORRENTS_INTERVAL = 300
# 刷流免费过期的检查时间间隔
BRUSH_STOP_TORRENTS_INTERVAL = 300
# 定时清除未识别的缓存时间间隔（小时）
META_DELETE_UNKNOWN_INTERVAL = 12
# 定时刷新壁纸的间隔（小时）
REFRESH_WALLPAPER_INTERVAL = 1
# fanart的api，用于拉取封面图片
FANART_MOVIE_API_URL = 'https://webservice.fanart.tv/v3/movies/%s?api_key=d2d31f9ecabea050fc7d68aa3146015f'
FANART_TV_API_URL = 'https://webservice.fanart.tv/v3/tv/%s?api_key=d2d31f9ecabea050fc7d68aa3146015f'
# 默认背景图地址
DEFAULT_TMDB_IMAGE = 'https://s3.bmp.ovh/imgs/2022/07/10/77ef9500c851935b.webp'
# TMDB域名地址
TMDB_API_DOMAINS = ['api.themoviedb.org', 'api.tmdb.org', 'tmdb.nastool.cn', 'tmdb.nastool.workers.dev']
TMDB_IMAGE_DOMAIN = 'image.tmdb.org'
# 添加下载时增加的标签，开始只监控NAStool添加的下载时有效
PT_TAG = "NASTOOL"
# 电影默认命名格式
DEFAULT_MOVIE_FORMAT = '{title} ({year})/{title} ({year})-{part} - {videoFormat}'
# 电视剧默认命名格式
DEFAULT_TV_FORMAT = '{title} ({year})/Season {season}/{title} - {season_episode}-{part} - 第 {episode} 集'
# 辅助识别参数
KEYWORD_SEARCH_WEIGHT_1 = [10, 3, 2, 0.5, 0.5]
KEYWORD_SEARCH_WEIGHT_2 = [10, 2, 1]
KEYWORD_SEARCH_WEIGHT_3 = [10, 2]
KEYWORD_STR_SIMILARITY_THRESHOLD = 0.2
KEYWORD_DIFF_SCORE_THRESHOLD = 30
KEYWORD_BLACKLIST = ['中字', '韩语', '双字', '中英', '日语', '双语', '国粤', 'HD', 'BD', '中日', '粤语', '完全版',
                     '法语', '西班牙语', 'HRHDTVAC3264', '未删减版', '未删减', '国语', '字幕组', '人人影视', 'www66ystv',
                     '人人影视制作', '英语', 'www6vhaotv', '无删减版', '完成版', '德意']

# WebDriver路径
WEBDRIVER_PATH = {
    "Docker": "/usr/lib/chromium/chromedriver",
    "Synology": "/var/packages/NASTool/target/bin/chromedriver"
}

# Xvfb虚拟显示路程
XVFB_PATH = [
    "/usr/bin/Xvfb",
    "/usr/local/bin/Xvfb"
]

# Chrome 路径
if os.environ.get('FLASK_DEBUG') == "1":
    CHROME_PATH = "/snap/bin/chromium"
else:
    CHROME_PATH = "/usr/lib/chromium/chromium"

# redis 配置
REDIS_HOST = "127.0.0.1"
REDIS_PORT = "6379"

# M-Team base url
MT_URL = 'https://api.m-team.io'

# sites.dat github
SITES_DATA_URL = "https://api.github.com/repos/linyuan0213/nas-tools-sites/releases/latest"

# 线程锁
lock = Lock()

# 全局实例
_CONFIG = None


def singleconfig(cls):
    def _singleconfig(*args, **kwargs):
        global _CONFIG
        if not _CONFIG:
            with lock:
                _CONFIG = cls(*args, **kwargs)
        return _CONFIG

    return _singleconfig


@singleconfig
class Config(object):
    _config = {}
    _config_path = None
    _user = None

    def __init__(self):
        self._config_path = os.environ.get('NASTOOL_CONFIG')
        if not os.environ.get('TZ'):
            os.environ['TZ'] = 'Asia/Shanghai'
        self.init_syspath()
        self.init_config()

    def _download_file(self, url, dest_path):
        """下载文件并校验"""
        try:
            # 获取GitHub Release信息
            api_response = requests.get(url, timeout=10, proxies=self.get_proxies())
            api_response.raise_for_status()
            release_info = api_response.json()
            
            # 获取实际文件下载URL
            download_url = release_info.get("assets")[0].get("browser_download_url")
            if not download_url:
                raise Exception("未找到下载URL")
            
            # 下载实际文件内容
            file_response = requests.get(download_url, timeout=30)
            file_response.raise_for_status()
            
            # 保存文件
            with open(dest_path, 'wb') as f:
                f.write(file_response.content)
                
            return True
        except Exception as e:
            print(f"【Config】下载文件失败: {str(e)}")
            return False

    def _get_sites_version(self, filepath):
        """获取sites.dat版本号"""
        try:
            with open(filepath, 'rb') as f:
                data = pickle.load(f)
                return data.get("version", "0")
        except:
            return "0"

    def _check_sites_update(self):
        """检查并更新sites.dat"""
        try:
            # 假GitHub Release URL
            release_url = SITES_DATA_URL
            local_path = os.path.join(os.path.dirname(self._config_path), "sites.dat")
            
            # 下载最新文件到临时位置
            temp_path = os.path.join(self.get_temp_path(), "sites.dat.tmp")
            if not self._download_file(release_url, temp_path):
                return False
                    
            new_version = self._get_sites_version(temp_path)
            current_version = self._get_sites_version(local_path) if os.path.exists(local_path) else "0"
            
            if new_version > current_version:
                shutil.move(temp_path, local_path)
                print(f"【Config】sites.dat 已更新到版本 {new_version}")
            else:
                os.remove(temp_path)
                print(f"【Config】当前版本 {current_version} 已是最新")
                
            return True
        except Exception as e:
            print(f"【Config】更新sites.dat失败: {str(e)}")
            return False

    def init_config(self):
        try:
            if not self._config_path:
                print("【Config】NASTOOL_CONFIG 环境变量未设置，程序无法工作，正在退出...")
                quit()
            if not os.path.exists(self._config_path):
                os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
                cfg_tp_path = os.path.join(self.get_inner_config_path(), "config.yaml")
                cfg_tp_path = cfg_tp_path.replace("\\", "/")
                shutil.copy(cfg_tp_path, self._config_path)
                print("【Config】config.yaml 配置文件不存在，已将配置文件模板复制到配置目录...")
            with open(self._config_path, mode='r', encoding='utf-8') as cf:
                try:
                    # 读取配置
                    print("正在加载配置：%s" % self._config_path)
                    self._config = ruamel.yaml.YAML().load(cf)
                except Exception as e:
                    print("【Config】配置文件 config.yaml 格式出现严重错误！请检查：%s" % str(e))
                    self._config = {}
            
            # 启动时检查sites.dat更新
            self._check_sites_update()
            # 复制sites.dat文件(使用版本号比较)
            src_sites = os.path.join(self.get_inner_config_path(), "sites.dat")
            dst_sites = os.path.join(os.path.dirname(self._config_path), "sites.dat")
            src_version = self._get_sites_version(src_sites)
            dst_version = self._get_sites_version(dst_sites) if os.path.exists(dst_sites) else "0"
            if not os.path.exists(dst_sites) or src_version > dst_version:
                shutil.copy2(src_sites, dst_sites)
                print(f"【Config】sites.dat 已更新到版本 {src_version}")

        except Exception as err:
            print("【Config】加载 config.yaml 配置出错：%s" % str(err))
            return False

    def init_syspath(self):
        with open(os.path.join(self.get_root_path(),
                               "third_party.txt"), "r") as f:
            for third_party_lib in f.readlines():
                module_path = os.path.join(self.get_root_path(),
                                           "third_party",
                                           third_party_lib.strip()).replace("\\", "/")
                if module_path not in sys.path:
                    sys.path.append(module_path)

    @property
    def current_user(self):
        return self._user

    @current_user.setter
    def current_user(self, user):
        self._user = user

    def get_proxies(self):
        return self.get_config('app').get("proxies")

    def get_ua(self):
        return self.get_config('app').get("user_agent") or DEFAULT_UA

    def get_config(self, node=None):
        if not node:
            return self._config
        return self._config.get(node, {})

    def save_config(self, new_cfg):
        self._config = new_cfg
        yaml = ruamel.yaml.YAML()

        # 检查数据是否可以正确序列化
        try:
            yaml.dump(new_cfg, io.StringIO())
        except Exception as e:
            raise ValueError(f"Invalid YAML data: {e}")

        # 文件锁防止并发写入
        lock_path = self._config_path + '.lock'
        with FileLock(lock_path):
            # 创建临时文件进行事务性写入
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as temp_file:
                yaml.dump(new_cfg, temp_file)
                temp_file_path = temp_file.name
            
            # 写入成功后用临时文件替换目标文件
            shutil.move(temp_file_path, self._config_path)

    def get_config_path(self):
        return os.path.dirname(self._config_path)

    def get_temp_path(self):
        return os.path.join(self.get_config_path(), "temp")

    @staticmethod
    def get_root_path():
        return os.path.dirname(os.path.realpath(__file__))

    def get_inner_config_path(self):
        return os.path.join(self.get_root_path(), "config")

    def get_script_path(self):
        return os.path.join(self.get_root_path(), "scripts", "sqls")

    def get_user_plugin_path(self):
        return os.path.join(self.get_config_path(), "plugins")

    def get_domain(self):
        domain = (self.get_config('app') or {}).get('domain')
        if domain and not domain.startswith('http'):
            domain = "http://" + domain
        if domain and str(domain).endswith("/"):
            domain = domain[:-1]
        return domain

    @staticmethod
    def get_timezone():
        return os.environ.get('TZ')

    @staticmethod
    def update_favtype(favtype):
        global RMT_FAVTYPE
        if favtype:
            RMT_FAVTYPE = favtype

    def get_tmdbapi_url(self):
        return f"https://{self.get_config('app').get('tmdb_domain') or TMDB_API_DOMAINS[0]}/3"

    def get_tmdbimage_url(self, path, prefix="w500"):
        if not path:
            return ""
        tmdb_image_url = self.get_config("app").get("tmdb_image_url")
        if tmdb_image_url:
            return tmdb_image_url + f"/t/p/{prefix}{path}"
        return f"https://{TMDB_IMAGE_DOMAIN}/t/p/{prefix}{path}"

    @property
    def category_path(self):
        category = self.get_config('media').get("category")
        if category:
            return os.path.join(Config().get_config_path(), f"{category}.yaml")
        return None
