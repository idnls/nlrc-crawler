"""
test_initial_v3.py — 초심사건 직접 fetch() 방식 검증 테스트.

확인 사항:
  1) 재심 AJAX 응답(detail.do)에서 hidden_vals 6개 파싱 (A)
  2) 직접 fetch()로 초심사건 조회: even_numb=medi_numb (B)
  3) 응답에 초심사건번호 포함 여부 (C)
  4) 초심 위원회명 파싱 (D)

이전 테스트(v2, v3)에서 확인된 root cause:
  - detailClick('JR')은 DOM의 stale 값(2022공정41 등)을 읽어 잘못된 요청 전송
  - 버튼 클릭을 완전히 우회하고 직접 fetch()를 구성하면 해결 가능

TARGET_CASE_TYPE: '부해' (JS계열) 또는 '차별' (DS계열) 모두 테스트 가능
"""

import asyncio
import re
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

TARGET_CASE_TYPE = '부해'   # '차별'로 바꿔서 DS계열도 확인 가능

def extract_committee_from_detail(detail_soup):
    TITLE_TAGS = ['th', 'caption', 'h2', 'h3', 'h4']
    for tag in detail_soup.find_all(TITLE_TAGS):
        text = tag.get_text(strip=True)
        if '노동위원회' in text and ('판정' in text or '결정' in text):
            match = re.match(r'(.+노동위원회)', text)
            if match:
                return match.group(1).strip()
    for tag in detail_soup.find_all(TITLE_TAGS):
        text = tag.get_text(strip=True)
        if '노동위원회' in text:
            match = re.match(r'(.+노동위원회)', text)
            if match:
                return match.group(1).strip()
    return None

def extract_hidden_vals_from_soup(soup):
    fields = ['medi_numb', 'begi_orga', 'even_numb', 'even_gubn', 'comm_code', 'midd_rscd']
    result = {}
    for field in fields:
        el = soup.find('input', {'id': field}) or soup.find('input', {'name': field})
        result[field] = el.get('value', '').strip() if el else ''
    # ★ 초심보기 버튼 onclick="detailClick('JR')"에서 초심 even_gubn 코드 추출
    initial_gubn = None
    for btn in soup.find_all('button'):
        if btn.get('title') == '초심보기' or btn.get_text(strip=True) == '초심보기':
            onclick = btn.get('onclick', '') or ''
            m = re.search(r"detailClick\(['\"](\w+)['\"]\)", onclick)
            if m:
                initial_gubn = m.group(1)
                break
    result['initial_gubn'] = initial_gubn or ''
    return result

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print(f"=== 테스트 시작: {TARGET_CASE_TYPE} 중앙노동위원회 재심 → 초심 직접 fetch ===\n")

        # ── 1. 검색 ─────────────────────────────────────────────────────────
        url = "https://nlrc.go.kr/nlrc/mainCase/judgment/search/index.do"
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.fill('#pQuery', TARGET_CASE_TYPE)
        await page.focus('#pQuery')
        await page.keyboard.press('Enter')

        try:
            async with page.expect_response(
                lambda res: "/list.do" in res.url and res.status == 200, timeout=30000
            ) as r:
                await page.click('.btnSearch')
                list_content = await (await r.value).text()
        except:
            list_content = await page.content()

        soup = BeautifulSoup(list_content, 'html.parser')
        dl_list = soup.find_all('dl', class_='C_Cts')
        print(f"[1] 목록 {len(dl_list)}건 발견")

        # ── 2. 중앙노동위원회 재심 + 초심사건 있는 케이스 선택 ───────────────
        target = None
        for dl in dl_list:
            dt = dl.find('dt', class_='tit')
            if not dt:
                continue
            a_tag = dt.find('a')
            if not a_tag:
                continue
            strong = a_tag.find('strong')
            committee = strong.get_text(strip=True) if strong else ''
            if '중앙' not in committee:
                continue
            spans = a_tag.find_all('span')
            case_number = spans[0].get_text(strip=True) if spans else ''
            if not case_number:
                continue
            target = {'case_number': case_number, 'committee': committee}
            break

        if not target:
            print("[!] 중앙노동위원회 재심 사건 미검출 → 종료")
            await browser.close()
            return

        print(f"[2] 선택된 재심 사건: {target['case_number']} / {target['committee']}\n")

        # ── 3. 재심 상세 AJAX 캡처 ──────────────────────────────────────────
        target_selector = f'a[data-k2="{target["case_number"]}"]'
        async with page.expect_response(
            lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000
        ) as r:
            await page.click(target_selector, force=True)
            detail_content = await (await r.value).text()

        detail_soup = BeautifulSoup(detail_content, 'html.parser')

        # ── [A] hidden_vals 파싱 ─────────────────────────────────────────────
        hidden_vals = extract_hidden_vals_from_soup(detail_soup)
        print("=== [A] 재심 HTML → hidden_vals ===")
        for k, v in hidden_vals.items():
            print(f"  {k:12s} = {v!r}")

        medi_numb    = hidden_vals.get('medi_numb', '')
        even_numb    = hidden_vals.get('even_numb', '')
        even_gubn    = hidden_vals.get('even_gubn', '')
        initial_gubn = hidden_vals.get('initial_gubn', '') or even_gubn  # ★ JR / DR 등

        if not medi_numb or medi_numb == even_numb or medi_numb == target['case_number']:
            print("\n[!] 초심사건번호(medi_numb) 없음 또는 재심과 동일 → 초심 없음")
            await browser.close()
            return

        print(f"\n  → 재심: {even_numb} / 초심: {medi_numb}")
        print(f"     재심 even_gubn: {even_gubn!r}  |  초심 initial_gubn: {initial_gubn!r}\n")

        # ── [B] 직접 fetch()로 초심사건 조회 ────────────────────────────────
        print("=== [B] 직접 fetch() → 초심사건 조회 ===")
        # ★ comm_code = begi_orga: detailClick('JR')이 내부적으로 하는 교체
        #   재심: comm_code='00'(중앙), begi_orga='09'(강원)
        #   초심 fetch: comm_code='09' → 강원지방노동위원회 라우팅
        begi_orga = hidden_vals.get('begi_orga', '')
        params = {
            'type':        'brjuPoin',
            'subType':     '06',
            'even_gubn':   initial_gubn,  # ★ 초심 코드(JR/DR)
            'comm_code':   begi_orga,     # ★ begi_orga 값으로 교체 (지방위원회 라우팅)
            'begi_orga':   begi_orga,
            'even_numb':   medi_numb,     # ★ 초심사건번호
            'resu_yeno':   '',
            'midd_rscd':   hidden_vals.get('midd_rscd', ''),
            'detail_gubn': '',
        }
        print(f"  요청 파라미터:")
        for k, v in params.items():
            print(f"    {k:12s} = {v!r}")

        initial_content = await page.evaluate("""
            async (params) => {
                const formData = new URLSearchParams(params);
                const response = await fetch(
                    '/nlrc/mainCase/judgment/search/detail.do',
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                        body: formData.toString()
                    }
                );
                return await response.text();
            }
        """, params)

        print(f"\n  응답 길이: {len(initial_content)}자")

        # ── [C] 응답 검증 ────────────────────────────────────────────────────
        print(f"\n=== [C] 응답 검증 ===")
        print(f"  초심사건번호({medi_numb}) 포함: {medi_numb in initial_content}")
        print(f"  재심사건번호({even_numb}) 포함: {even_numb in initial_content}")

        # 응답 HTML hidden_vals 확인
        resp_soup = BeautifulSoup(initial_content, 'html.parser')
        resp_hidden = extract_hidden_vals_from_soup(resp_soup)
        print(f"  응답 HTML hidden_vals:")
        for k, v in resp_hidden.items():
            print(f"    {k:12s} = {v!r}")

        # ── [D] 위원회 + 판정내용 파싱 ───────────────────────────────────────
        print(f"\n=== [D] 파싱 결과 ===")
        committee = extract_committee_from_detail(resp_soup)
        print(f"  위원회: {committee}")

        # 판정사항/결정사항 th
        matter_th = None
        for kw in ['판정사항', '결정사항']:
            matter_th = resp_soup.find('th', string=kw)
            if matter_th:
                break
        if matter_th:
            td = matter_th.find_next('td')
            matter_text = td.get_text(separator=' ', strip=True)[:100] if td else '없음'
            print(f"  판정사항 앞 100자: {matter_text}")
        else:
            print("  판정사항 th 미검출")

        # ── 최종 판정 ────────────────────────────────────────────────────────
        # ★ 중앙이어도 오류 아님: 법원 파기환송 등으로 3심 구조인 경우
        #   (지방→중앙초심→중앙재심), 초심이 중앙일 수 있음.
        #   판단 기준: 요청한 초심사건번호가 응답에 포함됐는가.
        print(f"\n{'='*50}")
        if medi_numb in initial_content and committee:
            print(f"✅ 성공: 초심사건({medi_numb}) 정상 조회")
            print(f"   위원회: {committee}")
            print(f"   (중앙 표시 시 → 법원 파기환송 등 3심 구조 케이스일 수 있음)")
        elif committee:
            print(f"❌ 실패: 초심사건번호({medi_numb})가 응답에 없음 → 파라미터 불일치")
            print(f"   응답의 위원회: {committee}")
        else:
            print(f"❌ 실패: 위원회 미검출")

        print(f"{'='*50}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
