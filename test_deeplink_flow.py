"""Test deeplink build + shorten — run on server: python test_deeplink_flow.py"""
import asyncio
import cityads_api as api

BASE = "https://yajgm.com/v2/click-30DL0-K7Oa2l-9PMzm-7fd1f59e?tl=1"
TARGET = "https://www.thebodyshop.com/products/shea-essentials-gift"

async def main():
    print("Шаг 1 — deeplink:", api.build_deeplink(BASE, TARGET))
    print("Шаг 2 — shorten...")
    short = await api.shorten_link(api.build_deeplink(BASE, TARGET))
    print("Шаг 2 — result:", short)

if __name__ == "__main__":
    asyncio.run(main())
