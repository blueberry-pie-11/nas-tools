import json
import uuid
import time
import requests

from app.utils.commons import SingletonMeta
from config import Config
import log

def generate_tab_id() -> str:
    """Generate a unique tab ID."""
    return str(uuid.uuid4())


class DrissionPageHelper(metaclass=SingletonMeta):
    
    def __init__(self):
        self.url = ""
        url = Config().get_config("laboratory").get('chrome_server_host')
        if url:
            if url.endswith("/"):
                self.url = url[:-1]
            self.url = url

    def get_status(self) -> bool:
        if self.url:
            return True
        return False

    def _request_with_retry(self, method, url, retries=3, delay=2, **kwargs):
        """通用的网络请求重试逻辑"""
        for attempt in range(retries):
            try:
                response = requests.request(method, url, **kwargs)
                return response
            except requests.exceptions.RequestException as e:
                log.warn(f"请求失败(重试 {attempt + 1}): {e}")
                if attempt < retries - 1:
                    time.sleep(delay)
                else:
                    log.error(f"所有重试失败，失败请求 {url}")
                    raise

    def get_page_html(self,
                      url: str,
                      cookies=None,
                      timeout: int = 120,
                      click_xpath: str = None,
                      delay: int = 2) -> str:
        
        if not self.get_status():
            return ""
        
        """获取HTML内容"""
        headers = {"Content-Type": "application/json"}
        tab_id = self.create_tab(url=url, cookies=cookies, timeout=timeout)

        # 延时多少秒停止加载网页
        time.sleep(delay)
        
        # 获取html内容
        html_url = f"{self.url}/tabs/{tab_id}/html"
        try:
            res_json = self._fetch_html(html_url, timeout)
        except Exception as e:
            log.error(f"获取html失败: {str(e)}")
            self.close_tab(tab_id)
            return ""

        # 处理点击事件
        if click_xpath:
            self._fetch_html(html_url, timeout)
            click_url = f"{self.url}/tabs/click/"
            click_data = json.dumps({
                "tab_name": tab_id,
                "selector": click_xpath
            }, separators=(',', ':'))
            try:
                response = self._request_with_retry(
                    method="POST",
                    url=click_url,
                    headers=headers,
                    data=click_data,
                    timeout=timeout
                )
            except Exception as e:
                log.error(f"点击标签页失败: {str(e)}")
                self.close_tab(tab_id=tab_id)
                return ""
            try:
                res_json = self._fetch_html(html_url, timeout)
            except Exception as e:
                log.error(f"点击之后获取html失败: {str(e)}")
                self.close_tab(tab_id)
                return ""

        # 关闭标签
        self.close_tab(tab_id)
        html_dict = json.loads(res_json)
        content = html_dict.get("html")
        return content

    def get_page_html_without_closetab(self, 
                                       tab_id: str, 
                                       timeout: int = 20, 
                                       is_refresh: bool = False,
                                       cf: bool = False,
                                       tab_category: str = "html") -> str:
        """
        获取html并保持标签页打开
        """
        
        if not self.get_status():
            return ""

        if is_refresh:
            self._refresh_tab(tab_id=tab_id)
        # 获取html内容
        html_url = f"{self.url}/tabs/{tab_id}/{tab_category}?cf={cf}"
        try:
            res_json = self._fetch_html(html_url, timeout)
        except Exception as e:
            log.error(f"获取html失败: {str(e)}")
            self.close_tab(tab_id)
            return ""

        html_dict = json.loads(res_json)
        content = html_dict.get("html")
        return content

    def _fetch_html(self, url: str, timeout: int) -> str:
        """返回html"""
        # 延迟加载，等待网页渲染完成
        try:
            response = self._request_with_retry(
                method="GET",
                url=url,
                timeout=timeout
            )
            return response.text
        except Exception as e:
            log.error(f"_fetch_html 失败: {str(e)}")
            raise

    def create_tab(self, url: str, cookies: str, timeout: int = 20)->str:
        
        if not self.get_status():
            return ""

        headers = {"Content-Type": "application/json"}
        tab_id = generate_tab_id()

        # 打开新标签
        tabs_url = f"{self.url}/tabs"
        open_tab_data = json.dumps({
            "url": url,
            "tab_name": tab_id,
            "cookie": cookies
        }, separators=(',', ':'))
        try:
            response = self._request_with_retry(
                method="POST",
                url=tabs_url,
                headers=headers,
                data=open_tab_data,
                timeout=timeout
            )
            if response.status_code not in (200, 400): 
                log.error(f"打开新标签页失败: {response.text}")
                return ""
            return tab_id
        except Exception as e:
            log.error(f"url: {url} 打开新标签页失败: {str(e)}")
            self.close_tab(tab_id=tab_id)
            return ""

    def get_cookie(self, tab_id: str, timeout: int = 20) -> str:
        """返回cookie"""
        # 延迟加载，等待网页渲染完成
        if not self.get_status():
            return ""

        try:
            response = self._request_with_retry(
                method="GET",
                url=f"{self.url}/tabs/{tab_id}/cookie",
                timeout=timeout
            )
            
            cookie_dict = response.json()
            content = cookie_dict.get("cookie")
            return content
        except Exception as e:
            log.error(f"get_cookie 失败: {str(e)}")
            raise

    def close_tab(self, tab_id: str):
        """关闭标签页"""
        
        if not self.get_status():
            return

        close_url = f"{self.url}/tabs/{tab_id}"
        try:
            self._request_with_retry(method="DELETE", url=close_url)
        except Exception as e:
            log.error(f"关闭标签页异常: {str(e)}")
            
    def _refresh_tab(self, tab_id: str):
        """刷新标签页"""
        try:
            response = self._request_with_retry(
                method="GET",
                url=f"{self.url}/tabs/{tab_id}/refresh"
            )
            
            status_dict = response.json()
            status = status_dict.get("code")
            return status
        except Exception as e:
            log.error(f"_refresh_tab 失败: {str(e)}")
            raise
        
    def input_on_element(self, tab_id: str, selector: str, input_str: str, timeout: str = 20): 

        if not self.get_status():
            return False

        headers = {"Content-Type": "application/json"}  
        click_url = f"{self.url}/tabs/input/"
        click_data = json.dumps({
            "tab_name": tab_id,
            "selector": selector,
            "input_str": input_str
        }, separators=(',', ':'))
        try:
            response = self._request_with_retry(
                method="POST",
                url=click_url,
                headers=headers,
                data=click_data,
                timeout=timeout
            )
            if response.json().get('code') == 0:
                return True
        except Exception as e:
            log.error(f"输入失败: {str(e)}")
            self.close_tab(tab_id=tab_id)
            return False

    def close_all_tabs(self):
        """关闭标签页"""
        if not self.get_status():
            return

        close_url = f"{self.url}/tabs/"
        try:
            self._request_with_retry(method="DELETE", url=close_url)
        except Exception as e:
            log.error(f"关闭标签页异常: {str(e)}")