import time
import logging
from DrissionPage import ChromiumPage, ChromiumOptions

# Подключаем твой рабочий экшен (убедись, что путь правильный для твоего проекта)
from workers.actions.action_update_profile import execute_update_profile

# Я сам перевел твои куки в правильный формат Python!
MY_COOKIES = [
    {"name": "datr", "value": "hsdvabaLFXims1PZzQrFtubo", "domain": ".instagram.com"},
    {"name": "ds_user_id", "value": "66998291245", "domain": ".instagram.com"},
    {"name": "csrftoken", "value": "sljjAJ4xxm6KXrD4r0enyuZCYGvnkuzQ", "domain": ".instagram.com"},
    {"name": "ig_did", "value": "F9841847-BA9C-405E-947B-9D2D9D937D2C", "domain": ".instagram.com"},
    {"name": "wd", "value": "1866x967", "domain": ".instagram.com"},
    {"name": "mid", "value": "aW_HhgAEAAG-A2g3tEgSI9ruSOL7", "domain": ".instagram.com"},
    {"name": "sessionid", "value": "66998291245%3Aok3FfItZNeROyP%3A14%3AAYjWSoa7VtqWcB50JNkzuibstF4GgaD2Q-M8EV__Fw", "domain": ".instagram.com"},
    {"name": "rur", "value": "\"LDC\\05466998291245\\0541809500115:01febfd5b892f76e7c384b87b824d8dd213636949fdce830baf06e6135d7b98077a12d7a\"", "domain": ".instagram.com"}
]

class DummyBrowser:
    """Фейковый класс браузера, чтобы обмануть наш строгий экшен"""
    def __init__(self, page):
        self.page = page

def run_test():
    print("🚀 Запускаем локальный тест без Celery и Прокси...")
    
    # 1. Настраиваем чистый локальный браузер
    co = ChromiumOptions()
    co.set_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    co.set_pref("profile.default_content_setting_values.notifications", 2)
    
    page = ChromiumPage(co)
    browser = DummyBrowser(page)
    
    try:
        # 2. Идем на нейтральную страницу и инжектим куки
        print("🍪 Инжектим куки...")
        page.get("https://www.instagram.com/robots.txt")
        time.sleep(2)
        
        for cookie in MY_COOKIES:
            page.set.cookies(cookie)
            
        print("✅ Куки загружены!")
        
        # 3. Запускаем наш production-экшен!
        print("🤖 Передаем управление Human Behavior Engine...")
        args = {
            "bio": "Тестовое био из локального скрипта #igcrm"
        }
        
        result = execute_update_profile(browser, args)
        print(f"✅ УСПЕХ! Результат: {result}")
        
    except Exception as e:
        print(f"❌ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("⏳ Тест завершен. Браузер закроется через 10 секунд...")
        time.sleep(10)
        page.quit()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_test()