import difflib
import os
import random
import re
import traceback
from functools import lru_cache

import zhconv
from lxml import etree

from app.helper.tmdb_blacklist_helper import TmdbBlacklistHelper
import log
from app.helper.openai_helper import OpenAiHelper
from app.media.meta.metainfo import MetaInfo
from app.media.tmdbv3api import TMDb, Search, Movie, TV, Person, Find, TMDbException, Discover, Trending, Episode, Genre
from app.utils import PathUtils, EpisodeFormat, RequestUtils, NumberUtils, StringUtils, cacheman
from app.utils.types import MediaType, MatchMode
from app.utils.tmdb_cache import TMDBCache
from config import Config, KEYWORD_BLACKLIST, KEYWORD_SEARCH_WEIGHT_3, KEYWORD_SEARCH_WEIGHT_2, KEYWORD_SEARCH_WEIGHT_1, \
    KEYWORD_STR_SIMILARITY_THRESHOLD, KEYWORD_DIFF_SCORE_THRESHOLD


class Media:
    # TheMovieDB
    tmdb = None
    search = None
    movie = None
    tv = None
    episode = None
    person = None
    find = None
    trending = None
    discover = None
    genre = None
    meta = None
    openai = None
    blacklist = None
    _rmt_match_mode = None
    _search_keyword = None
    _search_tmdbweb = None
    _chatgpt_enable = None
    _default_language = None

    def __init__(self):
        self.init_config()

    def init_config(self):
        app = Config().get_config('app')
        media = Config().get_config('media')
        laboratory = Config().get_config('laboratory')
        # 辅助查询
        self._search_keyword = laboratory.get("search_keyword")
        # WEB辅助
        self._search_tmdbweb = laboratory.get("search_tmdbweb")
        # ChatGPT辅助
        self._chatgpt_enable = laboratory.get("chatgpt_enable")
        # 默认语言
        self._default_language = media.get("tmdb_language", "zh") or "zh"
        # TMDB
        if app.get('rmt_tmdbkey'):
            # TMDB主体
            self.tmdb = TMDb()
            # 域名
            self.tmdb.domain = Config().get_tmdbapi_url()
            # 开启缓存
            self.tmdb.cache = True
            # APIKEY
            self.tmdb.api_key = app.get('rmt_tmdbkey')
            # 语种
            self.tmdb.language = self._default_language
            # 代理
            self.tmdb.proxies = Config().get_proxies()
            # 调试模式
            self.tmdb.debug = False
            # 查询对象
            self.search = Search()
            self.movie = Movie()
            self.tv = TV()
            self.episode = Episode()
            self.find = Find()
            self.person = Person()
            self.trending = Trending()
            self.discover = Discover()
            self.genre = Genre()
        # ChatGPT
        self.openai = OpenAiHelper()
        # 匹配模式
        rmt_match_mode = app.get('rmt_match_mode', 'normal')
        if rmt_match_mode:
            rmt_match_mode = rmt_match_mode.upper()
        else:
            rmt_match_mode = "NORMAL"
        if rmt_match_mode == "STRICT":
            self._rmt_match_mode = MatchMode.STRICT
        else:
            self._rmt_match_mode = MatchMode.NORMAL
        
        self.redis_cache = TMDBCache()
        self.blacklist = TmdbBlacklistHelper()

    def __set_language(self, language: str = ""):
        """
        设置语言
        :param language: zh/en
        """
        if not self.tmdb:
            return
        if language:
            self.tmdb.language = language
        else:
            self.tmdb.language = self._default_language

    @staticmethod
    def __compare_tmdb_names(file_name, tmdb_names):
        """
        比较文件名是否匹配，忽略大小写和特殊字符
        :param file_name: 识别的文件名或者种子名
        :param tmdb_names: TMDB返回的译名
        :return: True or False
        """
        if not file_name or not tmdb_names:
            return False
        if not isinstance(tmdb_names, list):
            tmdb_names = [tmdb_names]
        file_name = StringUtils.handler_special_chars(file_name).upper()
        for tmdb_name in tmdb_names:
            tmdb_name = StringUtils.handler_special_chars(tmdb_name).strip().upper()
            if file_name == tmdb_name:
                return True
        return False

    def __search_tmdb_allnames(self, mtype: MediaType, tmdb_id):
        """
        搜索tmdb中所有的标题和译名，用于名称匹配
        :param mtype: 类型：电影、电视剧、动漫
        :param tmdb_id: TMDB的ID
        :return: 所有译名的清单
        """
        if not mtype or not tmdb_id:
            return {}, []
        ret_names = []
        tmdb_info = self.get_tmdb_info(mtype=mtype, tmdbid=tmdb_id)
        if not tmdb_info:
            return tmdb_info, []
        if mtype == MediaType.MOVIE:
            alternative_titles = tmdb_info.get("alternative_titles", {}).get("titles", [])
            for alternative_title in alternative_titles:
                title = alternative_title.get("title")
                if title and title not in ret_names:
                    ret_names.append(title)
            translations = tmdb_info.get("translations", {}).get("translations", [])
            for translation in translations:
                title = translation.get("data", {}).get("title")
                if title and title not in ret_names:
                    ret_names.append(title)
        else:
            alternative_titles = tmdb_info.get("alternative_titles", {}).get("results", [])
            for alternative_title in alternative_titles:
                name = alternative_title.get("title")
                if name and name not in ret_names:
                    ret_names.append(name)
            translations = tmdb_info.get("translations", {}).get("translations", [])
            for translation in translations:
                name = translation.get("data", {}).get("name")
                if name and name not in ret_names:
                    ret_names.append(name)
        return tmdb_info, ret_names

    def __search_tmdb(self, file_media_name,
                      search_type,
                      first_media_year=None,
                      media_year=None,
                      season_number=None):
        """
        搜索tmdb中的媒体信息，匹配返回一条尽可能正确的信息
        :param file_media_name: 剑索的名称
        :param search_type: 类型：电影、电视剧、动漫
        :param first_media_year: 年份，如要是季集需要是首播年份(first_air_date)
        :param media_year: 当前季集年份
        :param season_number: 季集，整数
        :return: TMDB的INFO，同时会将search_type赋值到media_type中
        """
        if not self.search:
            return None
        if not file_media_name:
            return None
        # TMDB搜索
        info = {}
        if search_type == MediaType.MOVIE:
            year_range = [first_media_year]
            if first_media_year:
                # year_range.append(str(int(first_media_year) + 1))
                year_range.append(str(int(first_media_year) - 1))
            for year in year_range:
                log.debug(
                    f"【Meta】正在识别{search_type.value}：{file_media_name}, 年份={year} ...")
                info = self.__search_movie_by_name(file_media_name, year)
                if info:
                    info['media_type'] = MediaType.MOVIE
                    log.info("【Meta】%s 识别到 电影：TMDBID=%s, 名称=%s, 上映日期=%s" % (
                        file_media_name,
                        info.get('id'),
                        info.get('title'),
                        info.get('release_date')))
                    break
        else:
            # 有当前季和当前季集年份，使用精确匹配
            if media_year and season_number:
                log.debug(
                    f"【Meta】正在识别{search_type.value}：{file_media_name}, 季集={season_number}, 季集年份={media_year} ...")
                info = self.__search_tv_by_season(file_media_name,
                                                  media_year,
                                                  season_number)
            if not info:
                log.debug(
                    f"【Meta】正在识别{search_type.value}：{file_media_name}, 年份={StringUtils.xstr(first_media_year)} ...")
                info = self.__search_tv_by_name(file_media_name,
                                                first_media_year)
            if info:
                info['media_type'] = MediaType.TV
                log.info("【Meta】%s 识别到 电视剧：TMDBID=%s, 名称=%s, 首播日期=%s" % (
                    file_media_name,
                    info.get('id'),
                    info.get('name'),
                    info.get('first_air_date')))
        # 返回
        if not info:
            log.info("【Meta】%s 以年份 %s 在TMDB中未找到%s信息!" % (
                file_media_name, StringUtils.xstr(first_media_year), search_type.value if search_type else ""))
        return info

    def __search_movie_by_name(self, file_media_name, first_media_year):
        """
        根据名称查询电影TMDB匹配
        :param file_media_name: 识别的文件名或种子名
        :param first_media_year: 电影上映日期
        :return: 匹配的媒体信息
        """
        try:
            params = {"query": file_media_name}
            if first_media_year:
                params["year"] = first_media_year
            movies = self.search.movies(params)
            # 过滤黑名单
            media_blacklist, _ = self.blacklist.get_blacklist(count=1000)
            blacklist = [tmdb["tmdb_id"] for tmdb in media_blacklist]
            if movies and media_blacklist:
                movies = [movie for movie in movies 
                         if not (movie.get('id') and str(movie.get('id')) in blacklist)]
                log.debug(f"【Meta】过滤黑名单后剩余电影结果数: {len(movies)}")
        except (TMDbException, Exception) as err:
            log.error(f"【Meta】连接TMDB出错：{str(err)}")
            return None
            
        log.debug(f"【Meta】API返回：{str(self.search.total_results)}")
        if not movies:
            log.debug(f"【Meta】{file_media_name} 未找到相关电影信息!")
            return {}
            
        # 精确匹配
        for movie in movies:
            if not movie.get('release_date'):
                continue
                
            # 检查年份匹配
            year_matched = not first_media_year or movie.get('release_date', '')[:4] == str(first_media_year)
            if not year_matched:
                continue
                
            # 检查名称匹配
            if (self.__compare_tmdb_names(file_media_name, movie.get('title')) or
                self.__compare_tmdb_names(file_media_name, movie.get('original_title'))):
                return movie
                
        # 模糊匹配前5个结果
        for movie in movies[:5]:
            if first_media_year and movie.get('release_date', '')[:4] != str(first_media_year):
                continue
                
            info, names = self.__search_tmdb_allnames(MediaType.MOVIE, movie.get("id"))
            if self.__compare_tmdb_names(file_media_name, names):
                return info
                
        return {}

    def __search_tv_by_name(self, file_media_name, first_media_year):
        """
        根据名称查询电视剧TMDB匹配
        :param file_media_name: 识别的文件名或者种子名
        :param first_media_year: 电视剧的首播年份
        :return: 匹配的媒体信息
        """
        try:
            params = {"query": file_media_name}
            if first_media_year:
                params["first_air_date_year"] = first_media_year
            tvs = self.search.tv_shows(params)
            # 过滤黑名单
            media_blacklist, _ = self.blacklist.get_blacklist(count=1000)
            blacklist = [tmdb["tmdb_id"] for tmdb in media_blacklist]
            if tvs and blacklist:
                tvs = [tv for tv in tvs 
                      if not (tv.get('id') and str(tv.get('id')) in blacklist)]
                log.debug(f"【Meta】过滤黑名单后剩余电视剧结果数: {len(tvs)}")
        except (TMDbException, Exception) as err:
            log.error(f"【Meta】连接TMDB出错：{str(err)}")
            return None
            
        log.debug(f"【Meta】API返回：{str(self.search.total_results)}")
        if not tvs:
            log.debug(f"【Meta】{file_media_name} 未找到相关剧集信息!")
            return {}
        
        # 精确匹配
        for tv in tvs:
            if not tv.get('first_air_date'):
                continue
                
            # 检查年份匹配
            year_matched = not first_media_year or tv.get('first_air_date', '')[:4] == str(first_media_year)
            if not year_matched:
                continue
                
            # 检查名称匹配
            if (self.__compare_tmdb_names(file_media_name, tv.get('name')) or
                self.__compare_tmdb_names(file_media_name, tv.get('original_name'))):
                return tv
                
        # 模糊匹配前5个结果
        for tv in tvs[:5]:
            if first_media_year and tv.get('first_air_date', '')[:4] != str(first_media_year):
                continue
                
            info, names = self.__search_tmdb_allnames(MediaType.TV, tv.get("id"))
            if self.__compare_tmdb_names(file_media_name, names):
                return info
                
        return {}

    def __search_tv_by_season(self, file_media_name, media_year, season_number):
        """
        根据电视剧的名称和季的年份及序号匹配TMDB
        :param file_media_name: 识别的文件名或者种子名
        :param media_year: 季的年份
        :param season_number: 季序号
        :return: 匹配的媒体信息
        """
        def __season_match(tv_info, season_year):
            """检查季信息是否匹配"""
            if not tv_info:
                return False
            try:
                seasons = self.get_tmdb_tv_seasons(tv_info=tv_info)
                return any(
                    season.get("air_date", "")[:4] == str(season_year) 
                    and season.get("season_number") == int(season_number)
                    for season in seasons
                )
            except Exception as e:
                log.error(f"【Meta】连接TMDB出错：{e}")
                return False

        try:
            tvs = self.search.tv_shows({"query": file_media_name})
            # 过滤黑名单
            media_blacklist, _ = self.blacklist.get_blacklist(count=1000)
            blacklist = [tmdb["tmdb_id"] for tmdb in media_blacklist]
            if tvs and blacklist:
                tvs = [tv for tv in tvs 
                      if not (tv.get('id') and str(tv.get('id')) in blacklist)]
                log.debug(f"【Meta】过滤黑名单后剩余电视剧结果数: {len(tvs)}")
        except (TMDbException, Exception) as err:
            log.error(f"【Meta】连接TMDB出错：{str(err)}")
            return None

        if not tvs:
            log.debug(f"【Meta】{file_media_name} 未找到季{season_number}相关信息!")
            return {}

        # 精确匹配
        for tv in tvs:
            if (self.__compare_tmdb_names(file_media_name, tv.get('name')) or
                self.__compare_tmdb_names(file_media_name, tv.get('original_name'))) and \
               tv.get('first_air_date', '')[:4] == str(media_year):
                return tv

        # 模糊匹配前5个结果
        for tv in tvs[:5]:
            info, names = self.__search_tmdb_allnames(MediaType.TV, tv.get("id"))
            if self.__compare_tmdb_names(file_media_name, names) and \
               __season_match(info, media_year):
                return info

        return {}

    def __search_multi_tmdb(self, file_media_name):
        """
        根据名称同时查询电影和电视剧，不带年份
        :param file_media_name: 识别的文件名或种子名
        :return: 匹配的媒体信息
        """
        try:
            multis = self.search.multi({"query": file_media_name}) or []
        except (TMDbException, Exception) as err:
            log.error(f"【Meta】连接TMDB出错：{str(err)}")
            return None
            
        log.debug(f"【Meta】API返回：{str(self.search.total_results)}")
        if not multis:
            log.debug(f"【Meta】{file_media_name} 未找到相关媒体信息!")
            return {}
            
        # 精确匹配
        for multi in multis:
            if multi.get("media_type") == "movie":
                if (self.__compare_tmdb_names(file_media_name, multi.get('title')) or
                    self.__compare_tmdb_names(file_media_name, multi.get('original_title'))):
                    info = multi
                    break
            elif multi.get("media_type") == "tv":
                if (self.__compare_tmdb_names(file_media_name, multi.get('name')) or
                    self.__compare_tmdb_names(file_media_name, multi.get('original_name'))):
                    info = multi
                    break
        else:
            # 模糊匹配前5个结果
            for multi in multis[:5]:
                if multi.get("media_type") == "movie":
                    movie_info, names = self.__search_tmdb_allnames(MediaType.MOVIE, multi.get("id"))
                    if self.__compare_tmdb_names(file_media_name, names):
                        info = movie_info
                        break
                elif multi.get("media_type") == "tv":
                    tv_info, names = self.__search_tmdb_allnames(MediaType.TV, multi.get("id"))
                    if self.__compare_tmdb_names(file_media_name, names):
                        info = tv_info
                        break
            else:
                info = None
                
        # 设置媒体类型并返回
        if info:
            info['media_type'] = MediaType.MOVIE if info.get('media_type') in ['movie', MediaType.MOVIE] else MediaType.TV
        else:
            log.info(f"【Meta】{file_media_name} 在TMDB中未找到媒体信息!")
        return info

    @lru_cache(maxsize=512)
    def __search_chatgpt(self, file_name, mtype: MediaType):
        """
        通过ChatGPT对话识别文件名和集数等信息，重新查询TMDB数据
        :param file_name: 名称
        :param mtype: 媒体类型
        :return: 类型、季、集、TMDBINFO
        """
        def __failed():
            return mtype, None, None, {}

        def __failed_none():
            return mtype, None, None, None

        if not file_name:
            return __failed_none()

        log.info(f"【Meta】正在通过ChatGPT识别文件名：{file_name}")
        file_info = self.openai.get_media_name(file_name)
        
        if file_info is None:
            log.info("【Meta】ChatGPT识别出错，请检查是否设置OpenAI ApiKey！")
            return __failed_none()
        if not file_info:
            log.info("【Meta】ChatGPT识别失败！")
            return __failed()

        log.info(f"【Meta】ChatGPT识别结果：{file_info}")
        
        # 确定媒体类型
        if file_info.get("season") or file_info.get("episode"):
            mtype = MediaType.TV

        # 处理标题和年份
        title_parts = str(file_info.get("title", "")).split("/")
        file_title = title_parts[0].strip().replace(".", " ") if title_parts else None
        year_parts = str(file_info.get("year", "")).split("/")
        file_year = year_parts[0].strip() if year_parts else None

        if not file_title:
            return __failed()
        if file_year and not file_year.isdigit():
            file_year = None

        # 查询TMDB信息
        if mtype != MediaType.MOVIE or file_info.get("year"):
            tmdb_info = self.__search_tmdb(
                file_media_name=file_title,
                search_type=mtype,
                first_media_year=file_year
            )
        else:
            tmdb_info = self.__search_multi_tmdb(file_media_name=file_title)

        return (
            mtype,
            file_info.get("season"),
            file_info.get("episode"), 
            tmdb_info
        )

    @lru_cache(maxsize=512)
    def __search_tmdb_web(self, file_media_name, mtype: MediaType):
        """
        搜索TMDB网站，直接抓取结果，结果只有一条时才返回
        :param file_media_name: 名称
        :param mtype: 媒体类型
        :return: TMDB信息
        """
        if not file_media_name or StringUtils.is_chinese(file_media_name):
            return None

        log.info(f"【Meta】正在从TheDbMovie网站查询：{file_media_name}...")
        tmdb_url = f"https://www.themoviedb.org/search?query={file_media_name}"
        res = RequestUtils(timeout=5).get_res(url=tmdb_url)
        
        if not res or res.status_code != 200 or not res.text:
            return None

        try:
            html = etree.HTML(res.text)
            xpath = ("//a[@data-id and @data-media-type='tv']/@href" 
                    if mtype == MediaType.TV 
                    else "//a[@data-id]/@href")
            
            tmdb_links = [
                link for link in html.xpath(xpath)
                if link and (link.startswith("/tv") or link.startswith("/movie"))
            ]
            
            if len(tmdb_links) != 1:
                log.info(f"【Meta】{file_media_name} TMDB网站返回{'数据过多' if tmdb_links else '无'}结果")
                return None

            media_type = MediaType.TV if tmdb_links[0].startswith("/tv") else MediaType.MOVIE
            tmdbid = tmdb_links[0].split("/")[-1]
            tmdbinfo = self.get_tmdb_info(mtype=media_type, tmdbid=tmdbid)

            if not tmdbinfo or (mtype == MediaType.TV and tmdbinfo.get('media_type') != MediaType.TV):
                return None

            log.info(
                f"【Meta】{file_media_name} 从WEB识别到 {'电影' if media_type == MediaType.MOVIE else '电视剧'}："
                f"TMDBID={tmdbinfo.get('id')}, "
                f"名称={tmdbinfo.get('title' if media_type == MediaType.MOVIE else 'name')}, "
                f"日期={tmdbinfo.get('release_date' if media_type == MediaType.MOVIE else 'first_air_date')}"
            )
            return tmdbinfo

        except Exception as err:
            log.error(f"【Meta】TMDB网站查询出错：{str(err)}")
            return None

    def search_tmdb_person(self, name):
        """
        搜索TMDB演员信息
        """
        if not self.search:
            return []
        try:
            return self.__dict_tmdbpersons(self.search.people({"query": name}))
        except Exception as err:
            print(str(err))
            return []

    def get_tmdb_info(self, mtype: MediaType,
                      tmdbid,
                      language=None,
                      append_to_response=None,
                      chinese=True):
        """
        给定TMDB号，查询一条媒体信息
        :param mtype: 类型：电影、电视剧、动漫，为空时都查（此时用不上年份）
        :param tmdbid: TMDB的ID，有tmdbid时优先使用tmdbid，否则使用年份和标题
        :param language: 语种
        :param append_to_response: 附加信息
        :param chinese: 是否转换中文标题
        """
        # 先从缓存获取
        if not mtype:
            mtype = MediaType.UNKNOWN
        cached_info = self.redis_cache.get_tmdb_info(mtype, tmdbid, language)
        if cached_info:
            log.debug(f"【Meta】从缓存获取TMDB信息: {mtype.value}/{tmdbid}")
            return cached_info
            
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return None
        # 设置语言
        self.__set_language(language)
        if mtype == MediaType.MOVIE:
            tmdb_info = self.__get_tmdb_movie_detail(tmdbid, append_to_response)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.MOVIE
        else:
            tmdb_info = self.__get_tmdb_tv_detail(tmdbid, append_to_response)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.TV
        if tmdb_info:
            # 转换genreid
            tmdb_info['genre_ids'] = self.__get_genre_ids_from_detail(tmdb_info.get('genres'))
            # 转换中文标题
            if chinese:
                tmdb_info = self.__update_tmdbinfo_cn_title(tmdb_info)
        
        # 重置默认语言
        self.__set_language()

        # 设置缓存
        self.redis_cache.set_tmdb_info(mtype, tmdbid, tmdb_info, language)
        
        return tmdb_info

    def __update_tmdbinfo_cn_title(self, tmdb_info):
        """
        更新TMDB信息中的中文名称
        """
        # 查找中文名
        org_title = tmdb_info.get("title") \
            if tmdb_info.get("media_type") == MediaType.MOVIE \
            else tmdb_info.get("name")
        if not StringUtils.is_chinese(org_title) \
                and self._default_language == 'zh':
            cn_title = self.__get_tmdb_chinese_title(tmdbinfo=tmdb_info)
            if cn_title and cn_title != org_title:
                if tmdb_info.get("media_type") == MediaType.MOVIE:
                    tmdb_info['title'] = cn_title
                else:
                    tmdb_info['name'] = cn_title
        return tmdb_info

    def get_tmdb_infos(self, title, year=None, mtype: MediaType = None, language=None, page=1):
        """
        查询名称中有关键字的所有的TMDB信息并返回
        """
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return []
        if not title:
            return []
        # 设置语言
        self.__set_language(language)
        if not mtype and not year:
            results = self.__search_multi_tmdbinfos(title)
        else:
            if not mtype:
                results = list(
                    set(self.__search_movie_tmdbinfos(title, year)).union(set(self.__search_tv_tmdbinfos(title, year))))
                # 组合结果的情况下要排序
                results = sorted(results,
                                 key=lambda x: x.get("release_date") or x.get("first_air_date") or "0000-00-00",
                                 reverse=True)
            elif mtype == MediaType.MOVIE:
                results = self.__search_movie_tmdbinfos(title, year)
            else:
                results = self.__search_tv_tmdbinfos(title, year)
        
        # 重置默认语言
        self.__set_language()
        return results[(page - 1) * 20:page * 20]

    def __search_multi_tmdbinfos(self, title):
        """
        同时查询模糊匹配的电影、电视剧TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        multis = self.search.multi({"query": title}) or []
        for multi in multis:
            if multi.get("media_type") in ["movie", "tv"]:
                multi['media_type'] = MediaType.MOVIE if multi.get("media_type") == "movie" else MediaType.TV
                ret_infos.append(multi)
        return ret_infos

    def __search_movie_tmdbinfos(self, title, year):
        """
        查询模糊匹配的所有电影TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        if year:
            movies = self.search.movies({"query": title, "year": year}) or []
        else:
            movies = self.search.movies({"query": title}) or []
        for movie in movies:
            if title in movie.get("title"):
                movie['media_type'] = MediaType.MOVIE
                ret_infos.append(movie)
        return ret_infos

    def __search_tv_tmdbinfos(self, title, year):
        """
        查询模糊匹配的所有电视剧TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        if year:
            tvs = self.search.tv_shows({"query": title, "first_air_date_year": year}) or []
        else:
            tvs = self.search.tv_shows({"query": title}) or []
        for tv in tvs:
            if title in tv.get("name"):
                tv['media_type'] = MediaType.TV
                ret_infos.append(tv)
        return ret_infos


    def get_cache_info(self, meta_info):
        """
        根据名称查询是否已经有缓存
        """
        if not (meta_info and meta_info.tmdb_id):
            return {}
        return self.redis_cache.get_tmdb_info(meta_info.type, meta_info.tmdb_id) or {}

    def get_media_info(self, title,
                       subtitle=None,
                       mtype=None,
                       strict=None,
                       cache=True,
                       language=None,
                       chinese=True,
                       append_to_response=None):
        """
        只有名称信息，判别是电影还是电视剧并搜刮TMDB信息，用于种子名称识别
        :param title: 种子名称
        :param subtitle: 种子副标题
        :param mtype: 类型：电影、电视剧、动漫
        :param strict: 是否严格模式，为true时，不会再去掉年份再查一次
        :param cache: 是否使用缓存，默认TRUE
        :param language: 语言
        :param chinese: 原标题为英文时是否从别名中搜索中文名称
        :param append_to_response: 额外查询的信息
        :return: 带有TMDB信息的MetaInfo对象
        """
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return None
        if not title:
            return None
        # 设置语言
        self.__set_language(language)
        # 识别
        meta_info = MetaInfo(title, subtitle=subtitle)
        if not meta_info.get_name() or not meta_info.type:
            log.warn("【Rmt】%s 未识别出有效信息！" % meta_info.org_string)
            return None
        if mtype:
            meta_info.type = mtype

        # 尝试从缓存获取
        if cache:
            cached_info = self.redis_cache.get_media_info(
                title=meta_info.get_name(),
                year=meta_info.year,
                mtype=meta_info.type
            )
            if cached_info:
                log.info(f"【Meta】从缓存获取媒体信息: {meta_info.get_name()}")
                return cached_info
    
        if meta_info.type != MediaType.TV and not meta_info.year:
            file_media_info = self.__search_multi_tmdb(file_media_name=meta_info.get_name())
        else:
            if meta_info.type == MediaType.TV:
                # 确定是电视
                file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                     first_media_year=meta_info.year,
                                                     search_type=meta_info.type,
                                                     media_year=meta_info.year,
                                                     season_number=meta_info.begin_season
                                                     )
                if not file_media_info and meta_info.year and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                    # 非严格模式下去掉年份再查一次
                    file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                         search_type=meta_info.type
                                                         )
            else:
                # 有年份先按电影查
                file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                     first_media_year=meta_info.year,
                                                     search_type=MediaType.MOVIE
                                                     )
                # 没有再按电视剧查
                if not file_media_info:
                    file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                         first_media_year=meta_info.year,
                                                         search_type=MediaType.TV
                                                         )
                if not file_media_info and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                    # 非严格模式下去掉年份和类型再查一次
                    file_media_info = self.__search_multi_tmdb(file_media_name=meta_info.get_name())
        if not file_media_info and self._search_tmdbweb:
            # 从网站查询
            file_media_info = self.__search_tmdb_web(file_media_name=meta_info.get_name(),
                                                     mtype=meta_info.type)
        if not file_media_info and self._chatgpt_enable:
            # 通过ChatGPT查询
            mtype, seaons, episodes, file_media_info = self.__search_chatgpt(file_name=title,
                                                                             mtype=meta_info.type)
            # 修正类型和集数
            meta_info.type = mtype
            if not meta_info.get_season_string():
                meta_info.set_season(seaons)
            if not meta_info.get_episode_string():
                meta_info.set_episode(episodes)
        if not file_media_info and self._search_keyword:
            # 关键字猜测
            cache_name = cacheman["tmdb_supply"].get(meta_info.get_name())
            is_movie = False
            if not cache_name:
                cache_name, is_movie = self.__search_engine(meta_info.get_name())
                cacheman["tmdb_supply"].set(meta_info.get_name(), cache_name)
            if cache_name:
                log.info("【Meta】开始辅助查询：%s ..." % cache_name)
                if is_movie:
                    file_media_info = self.__search_tmdb(file_media_name=cache_name, search_type=MediaType.MOVIE)
                else:
                    file_media_info = self.__search_multi_tmdb(file_media_name=cache_name)
        # 补充全量信息
        if file_media_info and not file_media_info.get("genres"):
            file_media_info = self.get_tmdb_info(mtype=file_media_info.get("media_type"),
                                                 tmdbid=file_media_info.get("id"),
                                                 chinese=chinese,
                                                 append_to_response=append_to_response)
            
        # 重置默认语言
        self.__set_language()
 
        # 赋值TMDB信息并返回
        meta_info.set_tmdb_info(file_media_info)
        # 保存到缓存
        self.redis_cache.set_media_info(title=title, info=meta_info, year=meta_info.year, mtype=mtype)
        return meta_info

    def get_media_info_on_files(self,
                                file_list,
                                tmdb_info=None,
                                media_type=None,
                                season=None,
                                episode_format: EpisodeFormat = None,
                                language=None,
                                chinese=True,
                                append_to_response=None):
        """
        根据文件清单，搜刮TMDB信息，用于文件名称的识别
        :param file_list: 文件清单，如果是列表也可以是单个文件，也可以是一个目录
        :param tmdb_info: 如有传入TMDB信息则以该TMDB信息赋于所有文件，否则按名称从TMDB搜索，用于手工识别时传入
        :param media_type: 媒体类型：电影、电视剧、动漫，如有传入以该类型赋于所有文件，否则按名称从TMDB搜索并识别
        :param season: 季号，如有传入以该季号赋于所有文件，否则从名称中识别
        :param episode_format: EpisodeFormat
        :param language: 语言
        :param chinese: 原标题为英文时是否从别名中搜索中文名称
        :param append_to_response: 附加信息
        :return: 带有TMDB信息的每个文件对应的MetaInfo对象字典
        """
        # 存储文件路径与媒体的对应关系
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return {}
        # 设置语言
        self.__set_language(language)
        # 返回结果
        return_media_infos = {}
        # 不是list的转为list
        if not isinstance(file_list, list):
            file_list = [file_list]
        # 遍历每个文件，看得出来的名称是不是不一样，不一样的先搜索媒体信息
        for file_path in file_list:
            try:
                if not os.path.exists(file_path):
                    log.warn("【Meta】%s 不存在" % file_path)
                    continue
                # 解析媒体名称
                # 先用自己的名称
                file_name = os.path.basename(file_path)
                parent_name = os.path.basename(os.path.dirname(file_path))
                parent_parent_name = os.path.basename(PathUtils.get_parent_paths(file_path, 2))
                # 过滤掉蓝光原盘目录下的子文件
                if not os.path.isdir(file_path) \
                        and PathUtils.get_bluray_dir(file_path):
                    log.info("【Meta】%s 跳过蓝光原盘文件：" % file_path)
                    continue
                # 没有自带TMDB信息
                if not tmdb_info:
                    # 识别名称
                    meta_info = MetaInfo(title=file_name)
                    # 识别不到则使用上级的名称
                    if not meta_info.get_name() or not meta_info.year:
                        parent_info = MetaInfo(parent_name)
                        if not parent_info.get_name() or not parent_info.year:
                            parent_parent_info = MetaInfo(parent_parent_name)
                            parent_info.type = parent_parent_info.type if parent_parent_info.type and parent_info.type != MediaType.TV else parent_info.type
                            parent_info.cn_name = parent_info.cn_name if parent_info.cn_name else parent_parent_info.cn_name
                            parent_info.en_name = parent_info.en_name if parent_info.en_name else parent_parent_info.en_name
                            parent_info.year = parent_parent_info.year if parent_parent_info.year else parent_info.year
                            parent_info.begin_season = NumberUtils.max_ele(parent_info.begin_season,
                                                                           parent_parent_info.begin_season)
                        if not meta_info.get_name():
                            meta_info.cn_name = parent_info.cn_name
                            meta_info.en_name = parent_info.en_name
                        if not meta_info.year:
                            meta_info.year = parent_info.year
                        if parent_info.type and parent_info.type == MediaType.TV \
                                and meta_info.type != MediaType.TV:
                            meta_info.type = parent_info.type
                        if meta_info.type == MediaType.TV:
                            meta_info.begin_season = NumberUtils.max_ele(parent_info.begin_season,
                                                                         meta_info.begin_season)
                    if not meta_info.get_name() or not meta_info.type:
                        log.warn("【Rmt】%s 未识别出有效信息！" % meta_info.org_string)
                        continue
                    # 区配缓存及TMDB
                    file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                         first_media_year=meta_info.year,
                                                         search_type=meta_info.type,
                                                         media_year=meta_info.year,
                                                         season_number=meta_info.begin_season)
                    if not file_media_info:
                        if self._rmt_match_mode == MatchMode.NORMAL:
                            # 去掉年份再查一次，有可能是年份错误
                            file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                                 search_type=meta_info.type)
                    if not file_media_info and self._chatgpt_enable:
                        # 从ChatGPT查询
                        mtype, seaons, episodes, file_media_info = self.__search_chatgpt(file_name=file_path,
                                                                                         mtype=meta_info.type)
                        # 修正类型和集数
                        meta_info.type = mtype
                        if not meta_info.get_season_string():
                            meta_info.set_season(seaons)
                        if not meta_info.get_episode_string():
                            meta_info.set_episode(episodes)
                    if not file_media_info and self._search_keyword:
                        cache_name = cacheman["tmdb_supply"].get(meta_info.get_name())
                        is_movie = False
                        if not cache_name:
                            cache_name, is_movie = self.__search_engine(meta_info.get_name())
                            cacheman["tmdb_supply"].set(meta_info.get_name(), cache_name)
                        if cache_name:
                            log.info("【Meta】开始辅助查询：%s ..." % cache_name)
                            if is_movie:
                                file_media_info = self.__search_tmdb(file_media_name=cache_name,
                                                                     search_type=MediaType.MOVIE)
                            else:
                                file_media_info = self.__search_multi_tmdb(file_media_name=cache_name)
                    # 补全TMDB信息
                    if file_media_info and not file_media_info.get("genres"):
                        file_media_info = self.get_tmdb_info(mtype=file_media_info.get("media_type"),
                                                             tmdbid=file_media_info.get("id"),
                                                             chinese=chinese,
                                                             append_to_response=append_to_response)
 
                    # 赋值TMDB信息
                    meta_info.set_tmdb_info(file_media_info)
                # 自带TMDB信息
                else:
                    meta_info = MetaInfo(title=file_name, mtype=media_type)
                    meta_info.set_tmdb_info(tmdb_info)
                    if season and meta_info.type != MediaType.MOVIE:
                        meta_info.begin_season = int(season)
                    if episode_format:
                        begin_ep, end_ep, part = episode_format.split_episode(file_name)
                        if begin_ep is not None:
                            meta_info.begin_episode = begin_ep
                            meta_info.part = part
                        if end_ep is not None:
                            meta_info.end_episode = end_ep
                # 按文件路程存储
                return_media_infos[file_path] = meta_info
            except Exception as err:
                print(str(err))
                log.error("【Rmt】发生错误：%s - %s" % (str(err), traceback.format_exc()))
        # 重置默认语言
        self.__set_language()

        return return_media_infos

    def __dict_tmdbpersons(self, infos, chinese=True):
        """
        TMDB人员信息转为字典
        """
        if not infos:
            return []
        ret_infos = []
        for info in infos:
            if chinese:
                name = self.get_tmdbperson_chinese_name(person_id=info.get("id")) or info.get("name")
            else:
                name = info.get("name")
            tmdbid = info.get("id")
            image = Config().get_tmdbimage_url(info.get("profile_path"), prefix="h632") \
                if info.get("profile_path") else ""
            ret_infos.append({
                "id": tmdbid,
                "name": name,
                "role": info.get("name") if info.get("name") != name else "",
                "image": image
            })
        return ret_infos

    @staticmethod
    def __dict_tmdbinfos(infos, mtype=None, poster_filter=False):
        """
        TMDB电影信息转为字典
        """
        if not infos:
            return []
        ret_infos = []
        for info in infos:
            tmdbid = info.get("id")
            vote = round(float(info.get("vote_average")), 1) if info.get("vote_average") else 0,
            image = Config().get_tmdbimage_url(info.get("poster_path"))
            if poster_filter and not image:
                continue
            overview = info.get("overview")
            if mtype:
                media_type = mtype.value
                year = info.get("release_date")[0:4] if info.get(
                    "release_date") and mtype == MediaType.MOVIE else info.get(
                    "first_air_date")[0:4] if info.get(
                    "first_air_date") else ""
                typestr = 'MOV' if mtype == MediaType.MOVIE else 'TV'
                title = info.get("title") if mtype == MediaType.MOVIE else info.get("name")
            else:
                media_type = MediaType.MOVIE.value if info.get(
                    "media_type") == "movie" else MediaType.TV.value
                year = info.get("release_date")[0:4] if info.get(
                    "release_date") and info.get(
                    "media_type") == "movie" else info.get(
                    "first_air_date")[0:4] if info.get(
                    "first_air_date") else ""
                typestr = 'MOV' if info.get("media_type") == "movie" else 'TV'
                title = info.get("title") if info.get("media_type") == "movie" else info.get("name")

            ret_infos.append({
                'id': tmdbid,
                'orgid': tmdbid,
                'tmdbid': tmdbid,
                'title': title,
                'type': typestr,
                'media_type': media_type,
                'year': year,
                'vote': vote,
                'image': image,
                'overview': overview
            })

        return ret_infos

    def get_tmdb_hot_movies(self, page):
        """
        获取热门电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        return self.__dict_tmdbinfos(self.movie.popular(page), MediaType.MOVIE)

    def get_tmdb_hot_tvs(self, page):
        """
        获取热门电视剧
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.tv:
            return []
        return self.__dict_tmdbinfos(self.tv.popular(page), MediaType.TV)

    def get_tmdb_new_movies(self, page):
        """
        获取最新电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        return self.__dict_tmdbinfos(self.movie.now_playing(page), MediaType.MOVIE)

    def get_tmdb_new_tvs(self, page):
        """
        获取最新电视剧
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.tv:
            return []
        return self.__dict_tmdbinfos(self.tv.on_the_air(page), MediaType.TV)

    def get_tmdb_upcoming_movies(self, page):
        """
        获取即将上映电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        return self.__dict_tmdbinfos(self.movie.upcoming(page), MediaType.MOVIE)

    def get_tmdb_trending_all_week(self, page=1):
        """
        获取即将上映电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        return self.__dict_tmdbinfos(self.trending.all_week(page=page))

    def __get_tmdb_movie_detail(self, tmdbid, append_to_response=None):
        """
        获取电影的详情
        :param tmdbid: TMDB ID
        :return: TMDB信息
        """
        """
        {
          "adult": false,
          "backdrop_path": "/r9PkFnRUIthgBp2JZZzD380MWZy.jpg",
          "belongs_to_collection": {
            "id": 94602,
            "name": "穿靴子的猫（系列）",
            "poster_path": "/anHwj9IupRoRZZ98WTBvHpTiE6A.jpg",
            "backdrop_path": "/feU1DWV5zMWxXUHJyAIk3dHRQ9c.jpg"
          },
          "budget": 90000000,
          "genres": [
            {
              "id": 16,
              "name": "动画"
            },
            {
              "id": 28,
              "name": "动作"
            },
            {
              "id": 12,
              "name": "冒险"
            },
            {
              "id": 35,
              "name": "喜剧"
            },
            {
              "id": 10751,
              "name": "家庭"
            },
            {
              "id": 14,
              "name": "奇幻"
            }
          ],
          "homepage": "",
          "id": 315162,
          "imdb_id": "tt3915174",
          "original_language": "en",
          "original_title": "Puss in Boots: The Last Wish",
          "overview": "时隔11年，臭屁自大又爱卖萌的猫大侠回来了！如今的猫大侠（安东尼奥·班德拉斯 配音），依旧幽默潇洒又不拘小节、数次“花式送命”后，九条命如今只剩一条，于是不得不请求自己的老搭档兼“宿敌”——迷人的软爪妞（萨尔玛·海耶克 配音）来施以援手来恢复自己的九条生命。",
          "popularity": 8842.129,
          "poster_path": "/rnn30OlNPiC3IOoWHKoKARGsBRK.jpg",
          "production_companies": [
            {
              "id": 33,
              "logo_path": "/8lvHyhjr8oUKOOy2dKXoALWKdp0.png",
              "name": "Universal Pictures",
              "origin_country": "US"
            },
            {
              "id": 521,
              "logo_path": "/kP7t6RwGz2AvvTkvnI1uteEwHet.png",
              "name": "DreamWorks Animation",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "release_date": "2022-12-07",
          "revenue": 260725470,
          "runtime": 102,
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            },
            {
              "english_name": "Spanish",
              "iso_639_1": "es",
              "name": "Español"
            }
          ],
          "status": "Released",
          "tagline": "",
          "title": "穿靴子的猫2",
          "video": false,
          "vote_average": 8.614,
          "vote_count": 2291
        }
        """
        if not self.movie:
            return {}
        try:
            log.info("【Meta】正在查询TMDB电影：%s ..." % tmdbid)
            tmdbinfo = self.movie.details(tmdbid, append_to_response)
            if tmdbinfo:
                log.info(f"【Meta】{tmdbid} 查询结果：{tmdbinfo.get('title')}")
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def __get_tmdb_tv_detail(self, tmdbid, append_to_response=None):
        """
        获取电视剧的详情
        :param tmdbid: TMDB ID
        :return: TMDB信息
        """
        """
        {
          "adult": false,
          "backdrop_path": "/uDgy6hyPd82kOHh6I95FLtLnj6p.jpg",
          "created_by": [
            {
              "id": 35796,
              "credit_id": "5e84f06a3344c600153f6a57",
              "name": "Craig Mazin",
              "gender": 2,
              "profile_path": "/uEhna6qcMuyU5TP7irpTUZ2ZsZc.jpg"
            },
            {
              "id": 1295692,
              "credit_id": "5e84f03598f1f10016a985c0",
              "name": "Neil Druckmann",
              "gender": 2,
              "profile_path": "/bVUsM4aYiHbeSYE1xAw2H5Z1ANU.jpg"
            }
          ],
          "episode_run_time": [],
          "first_air_date": "2023-01-15",
          "genres": [
            {
              "id": 18,
              "name": "剧情"
            },
            {
              "id": 10765,
              "name": "Sci-Fi & Fantasy"
            },
            {
              "id": 10759,
              "name": "动作冒险"
            }
          ],
          "homepage": "https://www.hbo.com/the-last-of-us",
          "id": 100088,
          "in_production": true,
          "languages": [
            "en"
          ],
          "last_air_date": "2023-01-15",
          "last_episode_to_air": {
            "air_date": "2023-01-15",
            "episode_number": 1,
            "id": 2181581,
            "name": "当你迷失在黑暗中",
            "overview": "在一场全球性的流行病摧毁了文明之后，一个顽强的幸存者负责照顾一个 14 岁的小女孩，她可能是人类最后的希望。",
            "production_code": "",
            "runtime": 81,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
            "vote_average": 8,
            "vote_count": 33
          },
          "name": "最后生还者",
          "next_episode_to_air": {
            "air_date": "2023-01-22",
            "episode_number": 2,
            "id": 4071039,
            "name": "虫草变异菌",
            "overview": "",
            "production_code": "",
            "runtime": 55,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/jkUtYTmeap6EvkHI4n0j5IRFrIr.jpg",
            "vote_average": 10,
            "vote_count": 1
          },
          "networks": [
            {
              "id": 49,
              "name": "HBO",
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "origin_country": "US"
            }
          ],
          "number_of_episodes": 9,
          "number_of_seasons": 1,
          "origin_country": [
            "US"
          ],
          "original_language": "en",
          "original_name": "The Last of Us",
          "overview": "不明真菌疫情肆虐之后的美国，被真菌感染的人都变成了可怕的怪物，乔尔（Joel）为了换回武器答应将小女孩儿艾莉（Ellie）送到指定地点，由此开始了两人穿越美国的漫漫旅程。",
          "popularity": 5585.639,
          "poster_path": "/nOY3VBFO0VnlN9nlRombnMTztyh.jpg",
          "production_companies": [
            {
              "id": 3268,
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "name": "HBO",
              "origin_country": "US"
            },
            {
              "id": 11073,
              "logo_path": "/aCbASRcI1MI7DXjPbSW9Fcv9uGR.png",
              "name": "Sony Pictures Television Studios",
              "origin_country": "US"
            },
            {
              "id": 23217,
              "logo_path": "/kXBZdQigEf6QiTLzo6TFLAa7jKD.png",
              "name": "Naughty Dog",
              "origin_country": "US"
            },
            {
              "id": 115241,
              "logo_path": null,
              "name": "The Mighty Mint",
              "origin_country": "US"
            },
            {
              "id": 119645,
              "logo_path": null,
              "name": "Word Games",
              "origin_country": "US"
            },
            {
              "id": 125281,
              "logo_path": "/3hV8pyxzAJgEjiSYVv1WZ0ZYayp.png",
              "name": "PlayStation Productions",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "seasons": [
            {
              "air_date": "2023-01-15",
              "episode_count": 9,
              "id": 144593,
              "name": "第 1 季",
              "overview": "",
              "poster_path": "/aUQKIpZZ31KWbpdHMCmaV76u78T.jpg",
              "season_number": 1
            }
          ],
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            }
          ],
          "status": "Returning Series",
          "tagline": "",
          "type": "Scripted",
          "vote_average": 8.924,
          "vote_count": 601
        }
        """
        if not self.tv:
            return {}
        try:
            log.info("【Meta】正在查询TMDB电视剧：%s ..." % tmdbid)
            tmdbinfo = self.tv.details(tmdbid, append_to_response)
            if tmdbinfo:
                log.info(f"【Meta】{tmdbid} 查询结果：{tmdbinfo.get('name')}")
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def get_tmdb_tv_season_detail(self, tmdbid, season: int):
        """
        获取电视剧季的详情
        :param tmdbid: TMDB ID
        :param season: 季，数字
        :return: TMDB信息
        """
        """
        {
          "_id": "5e614cd3357c00001631a6ef",
          "air_date": "2023-01-15",
          "episodes": [
            {
              "air_date": "2023-01-15",
              "episode_number": 1,
              "id": 2181581,
              "name": "当你迷失在黑暗中",
              "overview": "在一场全球性的流行病摧毁了文明之后，一个顽强的幸存者负责照顾一个 14 岁的小女孩，她可能是人类最后的希望。",
              "production_code": "",
              "runtime": 81,
              "season_number": 1,
              "show_id": 100088,
              "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
              "vote_average": 8,
              "vote_count": 33,
              "crew": [
                {
                  "job": "Writer",
                  "department": "Writing",
                  "credit_id": "619c370063536a00619a08ee",
                  "adult": false,
                  "gender": 2,
                  "id": 35796,
                  "known_for_department": "Writing",
                  "name": "Craig Mazin",
                  "original_name": "Craig Mazin",
                  "popularity": 15.211,
                  "profile_path": "/uEhna6qcMuyU5TP7irpTUZ2ZsZc.jpg"
                },
              ],
              "guest_stars": [
                {
                  "character": "Marlene",
                  "credit_id": "63c4ca5e5f2b8d00aed539fc",
                  "order": 500,
                  "adult": false,
                  "gender": 1,
                  "id": 1253388,
                  "known_for_department": "Acting",
                  "name": "Merle Dandridge",
                  "original_name": "Merle Dandridge",
                  "popularity": 21.679,
                  "profile_path": "/lKwHdTtDf6NGw5dUrSXxbfkZLEk.jpg"
                }
              ]
            },
          ],
          "name": "第 1 季",
          "overview": "",
          "id": 144593,
          "poster_path": "/aUQKIpZZ31KWbpdHMCmaV76u78T.jpg",
          "season_number": 1
        }
        """
        if not self.tv:
            return {}
        try:
            log.info("【Meta】正在查询TMDB电视剧：%s，季：%s ..." % (tmdbid, season))
            tmdbinfo = self.tv.season_details(tmdbid, season)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return {}

    def get_tmdb_tv_seasons_byid(self, tmdbid):
        """
        根据TMDB查询TMDB电视剧的所有季
        """
        if not tmdbid:
            return []
        return self.get_tmdb_tv_seasons(
            tv_info=self.__get_tmdb_tv_detail(
                tmdbid=tmdbid
            )
        )

    @staticmethod
    def get_tmdb_tv_seasons(tv_info):
        """
        查询TMDB电视剧的所有季
        :param tv_info: TMDB 的季信息
        :return: 带有season_number、episode_count 的每季总集数的字典列表
        """
        """
        "seasons": [
            {
              "air_date": "2006-01-08",
              "episode_count": 11,
              "id": 3722,
              "name": "特别篇",
              "overview": "",
              "poster_path": "/snQYndfsEr3Sto2jOmkmsQuUXAQ.jpg",
              "season_number": 0
            },
            {
              "air_date": "2005-03-27",
              "episode_count": 9,
              "id": 3718,
              "name": "第 1 季",
              "overview": "",
              "poster_path": "/foM4ImvUXPrD2NvtkHyixq5vhPx.jpg",
              "season_number": 1
            }
        ]
        """
        if not tv_info:
            return []
        ret_info = []
        for info in tv_info.get("seasons") or []:
            if not info.get("season_number"):
                continue
            ret_info.append({
                "air_date": info.get("air_date"),
                "episode_count": info.get("episode_count"),
                "id": info.get("id"),
                "name": info.get("name"),
                "overview": info.get("overview"),
                "poster_path": Config().get_tmdbimage_url(info.get("poster_path")) if info.get("poster_path") else "",
                "season_number": info.get("season_number")
            })
        ret_info.reverse()
        return ret_info

    def get_tmdb_season_episodes(self, tmdbid, season: int):
        """
        :param: tmdbid: TMDB ID
        :param: season: 季号
        """
        """
        从TMDB的季集信息中获得某季的集信息
        """
        """
        "episodes": [
            {
              "air_date": "2023-01-15",
              "episode_number": 1,
              "id": 2181581,
              "name": "当你迷失在黑暗中",
              "overview": "在一场全球性的流行病摧毁了文明之后，一个顽强的幸存者负责照顾一个 14 岁的小女孩，她可能是人类最后的希望。",
              "production_code": "",
              "runtime": 81,
              "season_number": 1,
              "show_id": 100088,
              "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
              "vote_average": 8,
              "vote_count": 33
            },
          ]
        """
        if not tmdbid:
            return []
        season_info = self.get_tmdb_tv_season_detail(tmdbid=tmdbid, season=season)
        if not season_info:
            return []
        ret_info = []
        for info in season_info.get("episodes") or []:
            ret_info.append({
                "air_date": info.get("air_date"),
                "episode_number": info.get("episode_number"),
                "id": info.get("id"),
                "name": info.get("name"),
                "overview": info.get("overview"),
                "production_code": info.get("production_code"),
                "runtime": info.get("runtime"),
                "season_number": info.get("season_number"),
                "show_id": info.get("show_id"),
                "still_path": Config().get_tmdbimage_url(info.get("still_path")) if info.get("still_path") else "",
                "vote_average": info.get("vote_average")
            })
        ret_info.reverse()
        return ret_info

    def get_tmdb_backdrop(self, mtype, tmdbid):
        """
        获取TMDB的背景图
        """
        if not tmdbid:
            return ""
        tmdbinfo = self.get_tmdb_info(mtype=mtype,
                                      tmdbid=tmdbid,
                                      append_to_response="images",
                                      chinese=False)
        if not tmdbinfo:
            return ""
        results = self.get_tmdb_backdrops(tmdbinfo=tmdbinfo, original=False)
        return results[0] if results else ""

    @staticmethod
    def get_tmdb_backdrops(tmdbinfo, original=True):
        """
        获取TMDB的背景图
        """
        """
        {
          "backdrops": [
            {
              "aspect_ratio": 1.778,
              "height": 2160,
              "iso_639_1": "en",
              "file_path": "/qUroDlCDUMwRWbkyjZGB9THkMgZ.jpg",
              "vote_average": 5.312,
              "vote_count": 1,
              "width": 3840
            },
            {
              "aspect_ratio": 1.778,
              "height": 2160,
              "iso_639_1": "en",
              "file_path": "/iyxvxEQIfQjzJJTfszZxmH5UV35.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 3840
            },
            {
              "aspect_ratio": 1.778,
              "height": 720,
              "iso_639_1": "en",
              "file_path": "/8SRY6IcMKO1E5p83w7bjvcqklp9.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 1280
            },
            {
              "aspect_ratio": 1.778,
              "height": 1080,
              "iso_639_1": "en",
              "file_path": "/erkJ7OxJWFdLBOcn2MvIdhTLHTu.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 1920
            }
          ]
        }
        """
        if not tmdbinfo:
            return []
        prefix_url = Config().get_tmdbimage_url(r"%s", prefix="original") \
            if original else Config().get_tmdbimage_url(r"%s")
        backdrops = tmdbinfo.get("images", {}).get("backdrops") or []
        result = [prefix_url % backdrop.get("file_path") for backdrop in backdrops]
        result.append(prefix_url % tmdbinfo.get("backdrop_path"))
        return result

    @staticmethod
    def get_tmdb_season_episodes_num(tv_info, season: int):
        """
        从TMDB的季信息中获得具体季有多少集
        :param season: 季号，数字
        :param tv_info: 已获取的TMDB季的信息
        :return: 该季的总集数
        """
        if not tv_info:
            return 0
        seasons = tv_info.get("seasons")
        if not seasons:
            return 0
        for sea in seasons:
            if sea.get("season_number") == int(season):
                return int(sea.get("episode_count"))
        return 0

    @staticmethod
    def __dict_media_crews(crews):
        """
        字典化媒体工作人员
        """
        return [{
            "id": crew.get("id"),
            "gender": crew.get("gender"),
            "known_for_department": crew.get("known_for_department"),
            "name": crew.get("name"),
            "original_name": crew.get("original_name"),
            "popularity": crew.get("popularity"),
            "image": Config().get_tmdbimage_url(crew.get("profile_path"), prefix="h632"),
            "credit_id": crew.get("credit_id"),
            "department": crew.get("department"),
            "job": crew.get("job"),
            "profile": 'https://www.themoviedb.org/person/%s' % crew.get('id')
        } for crew in crews or []]

    @staticmethod
    def __dict_media_casts(casts):
        """
        字典化媒体演职人员
        """
        return [{
            "id": cast.get("id"),
            "gender": cast.get("gender"),
            "known_for_department": cast.get("known_for_department"),
            "name": cast.get("name"),
            "original_name": cast.get("original_name"),
            "popularity": cast.get("popularity"),
            "image": Config().get_tmdbimage_url(cast.get("profile_path"), prefix="h632"),
            "cast_id": cast.get("cast_id"),
            "role": cast.get("character"),
            "credit_id": cast.get("credit_id"),
            "order": cast.get("order"),
            "profile": 'https://www.themoviedb.org/person/%s' % cast.get('id')
        } for cast in casts or []]

    def get_tmdb_directors_actors(self, tmdbinfo):
        """
        查询导演和演员
        :param tmdbinfo: TMDB元数据
        :return: 导演列表，演员列表
        """
        """
        "cast": [
          {
            "adult": false,
            "gender": 2,
            "id": 3131,
            "known_for_department": "Acting",
            "name": "Antonio Banderas",
            "original_name": "Antonio Banderas",
            "popularity": 60.896,
            "profile_path": "/iWIUEwgn2KW50MssR7tdPeFoRGW.jpg",
            "cast_id": 2,
            "character": "Puss in Boots (voice)",
            "credit_id": "6052480e197de4006bb47b9a",
            "order": 0
          }
        ],
        "crew": [
          {
            "adult": false,
            "gender": 2,
            "id": 5524,
            "known_for_department": "Production",
            "name": "Andrew Adamson",
            "original_name": "Andrew Adamson",
            "popularity": 9.322,
            "profile_path": "/qqIAVKAe5LHRbPyZUlptsqlo4Kb.jpg",
            "credit_id": "63b86b2224b33300a0585bf1",
            "department": "Production",
            "job": "Executive Producer"
          }
        ]
        """
        if not tmdbinfo:
            return [], []
        _credits = tmdbinfo.get("credits")
        if not _credits:
            return [], []
        directors = []
        actors = []
        for cast in self.__dict_media_casts(_credits.get("cast")):
            if cast.get("known_for_department") == "Acting":
                actors.append(cast)
        for crew in self.__dict_media_crews(_credits.get("crew")):
            if crew.get("job") == "Director":
                directors.append(crew)
        return directors, actors

    def get_tmdb_cats(self, mtype, tmdbid):
        """
        获取TMDB的演员列表
        :param: mtype: 媒体类型
        :param: tmdbid: TMDBID
        """
        try:
            if mtype == MediaType.MOVIE:
                if not self.movie:
                    return []
                return self.__dict_media_casts(self.movie.credits(tmdbid).get("cast"))
            else:
                if not self.tv:
                    return []
                return self.__dict_media_casts(self.tv.credits(tmdbid).get("cast"))
        except Exception as err:
            print(str(err))
        return []

    @staticmethod
    def get_tmdb_genres_names(tmdbinfo):
        """
        从TMDB数据中获取风格名称
        """
        """
        "genres": [
            {
              "id": 16,
              "name": "动画"
            },
            {
              "id": 28,
              "name": "动作"
            },
            {
              "id": 12,
              "name": "冒险"
            },
            {
              "id": 35,
              "name": "喜剧"
            },
            {
              "id": 10751,
              "name": "家庭"
            },
            {
              "id": 14,
              "name": "奇幻"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        genres = tmdbinfo.get("genres") or []
        genres_list = [genre.get("name") for genre in genres]
        return ", ".join(genres_list) if genres_list else ""

    def get_tmdb_genres(self, mtype):
        """
        获取TMDB的风格列表
        :param: mtype: 媒体类型
        """
        if not self.genre:
            return []
        try:
            if mtype == MediaType.MOVIE:
                return self.genre.movie_list()
            else:
                return self.genre.tv_list()
        except Exception as err:
            print(str(err))
        return []

    @staticmethod
    def get_get_production_country_names(tmdbinfo):
        """
        从TMDB数据中获取制片国家名称
        """
        """
        "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "美国"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        countries = tmdbinfo.get("production_countries") or []
        countries_list = [country.get("name") for country in countries]
        return ", ".join(countries_list) if countries_list else ""

    @staticmethod
    def get_tmdb_production_company_names(tmdbinfo):
        """
        从TMDB数据中获取制片公司名称
        """
        """
        "production_companies": [
            {
              "id": 2,
              "logo_path": "/wdrCwmRnLFJhEoH8GSfymY85KHT.png",
              "name": "DreamWorks Animation",
              "origin_country": "US"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        companies = tmdbinfo.get("production_companies") or []
        companies_list = [company.get("name") for company in companies]
        return ", ".join(companies_list) if companies_list else ""

    @staticmethod
    def get_tmdb_crews(tmdbinfo, nums=None):
        """
        从TMDB数据中获取制片人员
        """
        if not tmdbinfo:
            return ""
        crews = tmdbinfo.get("credits", {}).get("crew") or []
        result = [{crew.get("name"): crew.get("job")} for crew in crews]
        if nums:
            return result[:nums]
        else:
            return result

    def get_tmdb_en_title(self, media_info):
        """
        获取TMDB的英文名称
        """
        en_info = self.get_tmdb_info(mtype=media_info.type,
                                     tmdbid=media_info.tmdb_id,
                                     language="en",
                                     chinese=False)
        if en_info:
            return en_info.get("title") if media_info.type == MediaType.MOVIE else en_info.get("name")
        return None

    def get_tmdb_zhtw_title(self, media_info):
        """
        获取TMDB的繁体中文名称
        """
        zhtw_info = self.get_tmdb_info(mtype=media_info.type,
                                     tmdbid=media_info.tmdb_id,
                                     language="zh-TW",
                                     chinese=False)
        if zhtw_info:
            return zhtw_info.get("title") if media_info.type == MediaType.MOVIE else zhtw_info.get("name")
        return None

    def get_episode_title(self, media_info, language=None):
        """
        获取剧集的标题
        """
        if media_info.type == MediaType.MOVIE:
            return None
        # 设置语言
        self.__set_language(language)
        if media_info.tmdb_id:
            if not media_info.begin_episode:
                return None
            episodes = self.get_tmdb_season_episodes(tmdbid=media_info.tmdb_id,
                                                     season=int(media_info.get_season_seq()))
            # 重置默认语言
            self.__set_language()
            for episode in episodes:
                if episode.get("episode_number") == media_info.begin_episode:
                    return episode.get("name")
        return None

    def get_movie_similar(self, tmdbid, page=1):
        """
        查询类似电影
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.similar(movie_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(movies, MediaType.MOVIE)
        except Exception as e:
            print(str(e))
            return []

    def get_movie_recommendations(self, tmdbid, page=1):
        """
        查询电影关联推荐
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.recommendations(movie_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(movies, MediaType.MOVIE)
        except Exception as e:
            print(str(e))
            return []

    def get_tv_similar(self, tmdbid, page=1):
        """
        查询类似电视剧
        """
        if not self.tv:
            return []
        try:
            tvs = self.tv.similar(tv_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(tvs, MediaType.TV)
        except Exception as e:
            print(str(e))
            return []

    def get_tv_recommendations(self, tmdbid, page=1):
        """
        查询电视剧关联推荐
        """
        if not self.tv:
            return []
        try:
            tvs = self.tv.recommendations(tv_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(tvs, MediaType.TV)
        except Exception as e:
            print(str(e))
            return []

    def get_tmdb_discover(self, mtype, params=None, page=1):
        """
        浏览电影、电视剧（复杂过滤条件）
        """
        if not self.discover:
            return []
        try:
            if mtype == MediaType.MOVIE:
                movies = self.discover.discover_movies(params=params, page=page)
                return self.__dict_tmdbinfos(infos=movies, mtype=mtype, poster_filter=True)
            elif mtype == MediaType.TV:
                tvs = self.discover.discover_tv_shows(params=params, page=page)
                return self.__dict_tmdbinfos(infos=tvs, mtype=mtype, poster_filter=True)
        except Exception as e:
            print(str(e))
        return []

    def get_tmdb_discover_movies_pages(self, params=None):
        """
        获取电影浏览的总页数
        """
        if not self.discover:
            return 0
        try:
            return self.discover.discover_movies_pages(params=params)
        except Exception as e:
            print(str(e))
        return 0

    def get_person_medias(self, personid, mtype=None, page=1):
        """
        查询人物相关影视作品
        """
        if not self.person:
            return []
        try:
            if mtype == MediaType.MOVIE:
                movies = self.person.movie_credits(person_id=personid) or []
                result = self.__dict_tmdbinfos(movies, mtype)
            elif mtype:
                tvs = self.person.tv_credits(person_id=personid) or []
                result = self.__dict_tmdbinfos(tvs, mtype)
            else:
                medias = self.person.combined_credits(person_id=personid) or []
                result = self.__dict_tmdbinfos(medias)
            return result[(page - 1) * 20: page * 20]
        except Exception as e:
            print(str(e))
        return []

    @staticmethod
    def __search_engine(feature_name):
        """
        辅助识别关键字
        """
        if not feature_name:
            return None, False

        # 清理输入名称
        feature_name = re.compile(r"^\w+字幕[组社]?", re.IGNORECASE).sub("", feature_name)
        for keyword in sorted(KEYWORD_BLACKLIST, key=lambda x: len(x), reverse=True):
            feature_name = feature_name.replace(keyword, " ")
        if not feature_name.strip():
            return None, False

        def calculate_scores(matches, results):
            """计算匹配项的分数"""
            length = len(matches)
            for i, match in enumerate(matches):
                if length < 5:
                    score = KEYWORD_SEARCH_WEIGHT_3[0] if i < 2 else KEYWORD_SEARCH_WEIGHT_3[1]
                elif length < 10:
                    if i < 2:
                        score = KEYWORD_SEARCH_WEIGHT_2[0]
                    else:
                        score = KEYWORD_SEARCH_WEIGHT_2[1] if i < (length // 2) else KEYWORD_SEARCH_WEIGHT_2[2]
                else:
                    if i < 2:
                        score = KEYWORD_SEARCH_WEIGHT_1[0]
                    else:
                        quarter = length // 4
                        half = length // 2
                        if i < quarter:
                            score = KEYWORD_SEARCH_WEIGHT_1[1]
                        elif i < half:
                            score = KEYWORD_SEARCH_WEIGHT_1[2]
                        elif i < (quarter + half):
                            score = KEYWORD_SEARCH_WEIGHT_1[3]
                        else:
                            score = KEYWORD_SEARCH_WEIGHT_1[4]
                
                key = match.lower()
                results[key] = results.get(key, 0) + score

        # 搜索Bing和百度
        bing_url = f"https://www.cn.bing.com/search?q={feature_name}&qs=n&form=QBRE&sp=-1"
        baidu_url = f"https://www.baidu.com/s?ie=utf-8&tn=baiduhome_pg&wd={feature_name}"
        
        results = {}
        is_movie = False
        
        # 处理Bing结果
        bing_res = RequestUtils(timeout=5).get_res(url=bing_url)
        if bing_res and bing_res.status_code == 200:
            html = etree.HTML(bing_res.text)
            if html:
                # 检查是否是电影
                title = html.xpath("//aside//h2[@class='b_entityTitle']/text()")
                if title:
                    clean_title = re.compile(r"\s*\(\d{4}\)$").sub("", title[0])
                    results[clean_title] = 200
                    if html.xpath("//aside//div[@data-feedbk-ids='Movie']"):
                        is_movie = True
                
                # 获取匹配的关键词
                selectors = [
                    "#sp_requery strong", "#sp_recourse strong", "#tile_link_cn strong",
                    ".b_ad .ad_esltitle~div strong", "h2 strong", ".b_caption p strong",
                    ".b_snippetBigText strong", ".recommendationsTableTitle+.b_slideexp strong",
                    ".recommendationsTableTitle+table strong", ".recommendationsTableTitle+ul strong",
                    ".pageRecoContainer .b_module_expansion_control strong",
                    ".pageRecoContainer .b_title>strong", ".b_rs strong", ".b_rrsr strong",
                    "#dict_ans strong", ".b_listnav>.b_ans_stamp>strong",
                    "#b_content #ans_nws .na_cnt strong", ".adltwrnmsg strong"
                ]
                matches = [
                    el.text for el in html.cssselect(",".join(selectors))
                    if el.text and difflib.SequenceMatcher(
                        None, feature_name, el.text).ratio() > KEYWORD_STR_SIMILARITY_THRESHOLD
                ]
                if matches:
                    calculate_scores(matches, results)

        # 处理百度结果
        baidu_res = RequestUtils(timeout=5).get_res(url=baidu_url)
        if baidu_res and baidu_res.status_code == 200:
            html = etree.HTML(baidu_res.text)
            if html:
                matches = [
                    el.text for el in html.cssselect("em")
                    if el.text and difflib.SequenceMatcher(
                        None, feature_name, el.text).ratio() > KEYWORD_STR_SIMILARITY_THRESHOLD
                ]
                if matches:
                    calculate_scores(matches, results)

        if not results:
            return None, False

        # 排序并选择最佳匹配
        sorted_results = sorted(results.items(), key=lambda x: x[1], reverse=True)
        log.info(f"【Meta】推断关键字为：{[k[0] for k in sorted_results[:4]]}")

        if len(sorted_results) == 1:
            return sorted_results[0][0], is_movie

        first, second = sorted_results[:2]
        
        # 选择逻辑
        if second[0].find(first[0]) > -1:
            if first[1] >= 100:  # 高分直接选择
                keyword = first[0]
            elif first[1] - second[1] > KEYWORD_DIFF_SCORE_THRESHOLD:  # 分差大选高分
                keyword = first[0]
            elif second[0].replace(first[0], "").strip() == first[0]:  # 重复内容
                keyword = first[0]
            elif first[0].isdigit():  # 纯数字不选
                keyword = second[0]
            else:
                keyword = second[0]
        else:
            keyword = first[0]

        log.info(f"【Meta】选择关键字为：{keyword}")
        return keyword, is_movie

    @staticmethod
    def __get_genre_ids_from_detail(genres):
        """
        从TMDB详情中获取genre_id列表
        """
        if not genres:
            return []
        genre_ids = []
        for genre in genres:
            genre_ids.append(genre.get('id'))
        return genre_ids

    @staticmethod
    def __get_tmdb_chinese_title(tmdbinfo):
        """
        从别名中获取中文标题
        """
        if not tmdbinfo:
            return None
        if tmdbinfo.get("media_type") == MediaType.MOVIE:
            alternative_titles = tmdbinfo.get("alternative_titles", {}).get("titles", [])
        else:
            alternative_titles = tmdbinfo.get("alternative_titles", {}).get("results", [])
        for alternative_title in alternative_titles:
            iso_3166_1 = alternative_title.get("iso_3166_1")
            if iso_3166_1 == "CN":
                title = alternative_title.get("title")
                if title and StringUtils.is_chinese(title) and zhconv.convert(title, "zh-hans") == title:
                    return title
        return tmdbinfo.get("title") if tmdbinfo.get("media_type") == MediaType.MOVIE else tmdbinfo.get("name")

    def get_tmdbperson_chinese_name(self, person_id=None, person_info=None):
        """
        查询TMDB人物中文名称
        """
        if not self.person:
            return ""
        if not person_info and not person_id:
            return ""
        # 返回中文名
        name = ""
        # 所有别名
        alter_names = []
        try:
            if not person_info:
                person_info = self.person.details(person_id)
            if person_info:
                aka_names = person_info.get("also_known_as", []) or []
            else:
                return ""
        except Exception as err:
            print(str(err))
            return ""
        for aka_name in aka_names:
            if StringUtils.is_chinese(aka_name):
                alter_names.append(aka_name)
        if len(alter_names) == 1:
            name = alter_names[0]
        elif len(alter_names) > 1:
            for alter_name in alter_names:
                if alter_name == zhconv.convert(alter_name, 'zh-hans'):
                    name = alter_name
        return name

    def get_tmdbperson_aka_names(self, person_id):
        """
        查询人物又名
        """
        if not self.person:
            return []
        try:
            aka_names = self.person.details(person_id).get("also_known_as", []) or []
            return aka_names
        except Exception as err:
            print(str(err))
            return []

    def get_random_discover_backdrop(self):
        """
        获取TMDB热门电影随机一张背景图
        """
        if not self.discover:
            return ""
        try:
            medias = self.discover.discover_movies(params={"sort_by": "popularity.desc"})
            if medias:
                # 随机一个电影
                media = random.choice(medias)
                img_url = Config().get_tmdbimage_url(media.get("backdrop_path"), prefix="original") \
                    if media.get("backdrop_path") else ''
                img_title = media.get('title', '')
                img_link = f"https://www.themoviedb.org/movie/{media.get('id')}" if media.get('id') else ''
                return img_url, img_title, img_link
        except Exception as err:
            print(str(err))
        return '', '', ''

    @staticmethod
    def merge_media_info(target, source):
        """
        将soruce中有效的信息合并到target中并返回
        """
        target.set_tmdb_info(source.tmdb_info)
        target.fanart_poster = source.get_poster_image()
        target.fanart_backdrop = source.get_backdrop_image()
        target.set_download_info(download_setting=source.download_setting,
                                 save_path=source.save_path)
        return target

    def get_tmdbid_by_imdbid(self, imdbid):
        """
        根据IMDBID查询TMDB信息
        """
        if not self.find:
            return None
        try:
            result = self.find.find_by_imdbid(imdbid) or {}
            tmdbinfo = result.get('movie_results') or result.get("tv_results")
            if tmdbinfo:
                tmdbinfo = tmdbinfo[0]
                return tmdbinfo.get("id")
        except Exception as err:
            print(str(err))
        return None

    @staticmethod
    def get_detail_url(mtype, tmdbid):
        """
        获取TMDB/豆瓣详情页地址
        """
        if not tmdbid:
            return ""
        if str(tmdbid).startswith("DB:"):
            return "https://movie.douban.com/subject/%s" % str(tmdbid).replace("DB:", "")
        elif mtype == MediaType.MOVIE:
            return "https://www.themoviedb.org/movie/%s" % tmdbid
        else:
            return "https://www.themoviedb.org/tv/%s" % tmdbid

    def get_episode_images(self, tv_id, season_id, episode_id, orginal=False):
        """
        获取剧集中某一集封面
        """
        if not self.episode:
            return ""
        if not tv_id or not season_id or not episode_id:
            return ""
        res = self.episode.images(tv_id, season_id, episode_id)
        if res:
            if orginal:
                return Config().get_tmdbimage_url(res[-1].get("file_path"), prefix="original")
            else:
                return Config().get_tmdbimage_url(res[-1].get("file_path"))
        else:
            return ""

    def get_tmdb_factinfo(self, media_info):
        """
        获取TMDB发布信息
        """
        result = []
        if media_info.vote_average:
            result.append({"评分": media_info.vote_average})
        if media_info.original_title:
            result.append({"原始标题": media_info.original_title})
        status = media_info.tmdb_info.get("status")
        if status:
            result.append({"状态": status})
        if media_info.release_date:
            result.append({"上映日期": media_info.release_date})
        revenue = media_info.tmdb_info.get("revenue")
        if revenue:
            result.append({"收入": StringUtils.str_amount(revenue)})
        budget = media_info.tmdb_info.get("budget")
        if budget:
            result.append({"成本": StringUtils.str_amount(budget)})
        if media_info.original_language:
            result.append({"原始语言": media_info.original_language})
        production_country = self.get_get_production_country_names(tmdbinfo=media_info.tmdb_info)
        if media_info.networks:
            result.append({"电视网": media_info.networks})
        if production_country:
            result.append({"出品国家": production_country}),
        production_company = self.get_tmdb_production_company_names(tmdbinfo=media_info.tmdb_info)
        if production_company:
            result.append({"制作公司": production_company})

        return result
