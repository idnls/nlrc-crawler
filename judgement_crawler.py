"""
test_initial_v3.py — 중앙노동위원회 재심 사건 1개에 대해 초심사건 정보를
올바르게 가져오는지 집중 테스트.

진단 항목:
  A) 재심 AJAX 응답에서 파싱된 hidden_vals 전체 6개 필드
  B) 초심보기 클릭 직전 DOM 상태 (어떤 필드가 존재하는가)
  C) DOM 덮어쓰기 후 실제 어떤 필드가 설정됐는가
  D) 초심보기 클릭 시 실제 전송되는 request body (POST 파라미터)
  E) 캡처된 응답에 초심사건번호가 포함돼 있는가
  F) 최종 파싱 결과 (위원회, 판정내용)

GitHub Actions 로그에서 A~F를 확인하고 어느 단계에서 실패하는지 판단.
"""

import asyncio
import re
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

TARGET_CASE_TYPE = '부해'   # 부해 = JS계열 / 차별 = DS계열 — 둘 다 테스트 가능

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
    return result

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print(f"=== 테스트 시작: {TARGET_CASE_TYPE} 사건 중앙노동위원회 재심 1개 ===\n")

        # ── 1. 목록 페이지 로드 및 검색 ────────────────────────────────────
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

        # ── 2. 중앙노동위원회 재심 사건 1개 선택 ──────────────────────────
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

        # ── 3. 재심 상세 AJAX 캡처 ─────────────────────────────────────────
        target_selector = f'a[data-k2="{target["case_number"]}"]'
        async with page.expect_response(
            lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000
        ) as r:
            await page.click(target_selector, force=True)
            detail_content = await (await r.value).text()

        detail_soup = BeautifulSoup(detail_content, 'html.parser')

        # ── A) hidden_vals 전체 출력 ───────────────────────────────────────
        hidden_vals = extract_hidden_vals_from_soup(detail_soup)
        print("=== [A] 재심 AJAX HTML에서 파싱된 hidden_vals ===")
        for k, v in hidden_vals.items():
            print(f"  {k} = {v!r}")

        medi_numb = hidden_vals.get('medi_numb', '')
        even_numb = hidden_vals.get('even_numb', '')

        if not medi_numb or medi_numb == even_numb or medi_numb == target['case_number']:
            print("\n[!] 초심사건번호(medi_numb) 미검출 또는 재심과 동일 → 초심 없음으로 판단 후 종료")
            await browser.close()
            return

        print(f"\n  → 초심사건번호: {medi_numb}  /  재심사건번호: {even_numb}\n")

        # ── 2초 대기 (배경 요청 안정화) ────────────────────────────────────
        print("[*] 2초 대기 (배경 요청 안정화)...")
        await asyncio.sleep(2)

        # ── B) 덮어쓰기 직전 DOM 상태 ──────────────────────────────────────
        dom_before = await page.evaluate("""
            () => {
                const fields = ['medi_numb', 'begi_orga', 'even_numb', 'even_gubn', 'comm_code', 'midd_rscd'];
                const result = {};
                fields.forEach(f => {
                    const el = document.querySelector('#' + f + ', input[name="' + f + '"]');
                    result[f] = el ? el.value : null;
                });
                // 팝업 내 모든 hidden input도 확인
                const allHidden = Array.from(document.querySelectorAll('.layer-wrap input[type=hidden], .layer-cont input[type=hidden]'))
                                        .map(el => ({ id: el.id, name: el.name, value: el.value }));
                return { fields: result, allHidden };
            }
        """)
        print("=== [B] 덮어쓰기 직전 DOM 상태 ===")
        for k, v in dom_before['fields'].items():
            print(f"  {k} = {v!r}")
        print(f"  팝업 내 hidden inputs ({len(dom_before['allHidden'])}개):")
        for h in dom_before['allHidden']:
            print(f"    id={h['id']!r} name={h['name']!r} value={h['value']!r}")

        # ── C) DOM 덮어쓰기 ────────────────────────────────────────────────
        set_result = await page.evaluate("""
            (vals) => {
                const set_fields = [];
                const not_found = [];
                for (const [field, value] of Object.entries(vals)) {
                    const el = document.querySelector('#' + field + ', input[name="' + field + '"]');
                    if (el) {
                        el.value = value;
                        set_fields.push({ field, old: el.defaultValue, new: el.value });
                    } else {
                        not_found.push(field);
                    }
                }
                return { set_fields, not_found };
            }
        """, hidden_vals)
        print("\n=== [C] DOM 덮어쓰기 결과 ===")
        print(f"  설정 성공 ({len(set_result['set_fields'])}개): {[x['field'] for x in set_result['set_fields']]}")
        print(f"  미검출 ({len(set_result['not_found'])}개): {set_result['not_found']}")

        # ── 초심보기 버튼 확인 ─────────────────────────────────────────────
        btn_info = await page.evaluate("""
            () => {
                const btn = Array.from(document.querySelectorAll('button')).find(
                    b => b.title === '초심보기' ||
                         (b.textContent && b.textContent.trim().includes('초심보기'))
                );
                return btn ? { found: true, title: btn.title, onclick: btn.getAttribute('onclick'), text: btn.textContent.trim() } : { found: false };
            }
        """)
        print(f"\n  초심보기 버튼: {btn_info}")

        if not btn_info['found']:
            print("[!] 초심보기 버튼 없음 → 종료")
            await browser.close()
            return

        # ── D) 초심보기 클릭 시 실제 request body 캡처 ─────────────────────
        print("\n=== [D] 초심보기 클릭 request body ===")
        captured_requests = []

        def on_request(req):
            if '/detail.do' in req.url:
                try:
                    captured_requests.append({
                        'url': req.url,
                        'method': req.method,
                        'body': req.post_data
                    })
                except Exception as e:
                    captured_requests.append({'error': str(e)})

        page.on('request', on_request)

        # ── E) expect_response로 응답 캡처 ────────────────────────────────
        initial_content = None
        try:
            async with page.expect_response(
                lambda res: "/detail.do" in res.url and res.status == 200,
                timeout=15000
            ) as resp_info:
                await page.evaluate("""
                    () => {
                        const btn = Array.from(document.querySelectorAll('button')).find(
                            b => b.title === '초심보기' ||
                                 (b.textContent && b.textContent.trim().includes('초심보기'))
                        );
                        if (btn) btn.click();
                    }
                """)
                initial_content = await (await resp_info.value).text()
        except Exception as e:
            print(f"  ⚠️ expect_response 실패: {e}")

        page.remove_listener('request', on_request)

        # 1초 추가 대기 후 남은 요청 확인
        await asyncio.sleep(1)

        print(f"  클릭 이후 /detail.do 요청 수: {len(captured_requests)}")
        for i, req in enumerate(captured_requests):
            print(f"  [요청 {i+1}] url={req.get('url','?')}")
            body = req.get('body', '')
            if body:
                # URL 디코딩해서 출력
                from urllib.parse import unquote_plus
                decoded = unquote_plus(body)
                print(f"          body: {decoded[:300]}")

        # ── E) 응답 검증 ───────────────────────────────────────────────────
        print(f"\n=== [E] 캡처된 응답 검증 ===")
        if initial_content:
            print(f"  응답 길이: {len(initial_content)}자")
            print(f"  초심사건번호({medi_numb}) 포함 여부: {medi_numb in initial_content}")
            print(f"  재심사건번호({even_numb}) 포함 여부: {even_numb in initial_content}")

            # ── F) 파싱 결과 ─────────────────────────────────────────────
            print(f"\n=== [F] 파싱 결과 ===")
            initial_soup = BeautifulSoup(initial_content, 'html.parser')
            committee = extract_committee_from_detail(initial_soup)
            print(f"  위원회: {committee}")

            # 응답 내 hidden inputs 확인
            resp_hidden = extract_hidden_vals_from_soup(initial_soup)
            print(f"  응답 HTML hidden_vals:")
            for k, v in resp_hidden.items():
                print(f"    {k} = {v!r}")

            # 위원회 판정요지 th 태그들
            th_texts = [th.get_text(strip=True) for th in initial_soup.find_all('th') if '노동위원회' in th.get_text()]
            print(f"  위원회 관련 th 태그들: {th_texts}")

            # 응답이 올바른 초심 사건인지 판단
            if medi_numb in initial_content:
                print(f"\n✅ 초심사건번호({medi_numb})가 응답에 포함됨 → 올바른 초심 응답")
            else:
                print(f"\n❌ 초심사건번호({medi_numb})가 응답에 없음 → 잘못된 응답 (배경 요청 캡처 가능성)")
                # 응답 앞부분 출력
                print(f"  응답 앞 200자: {initial_content[:200]!r}")
        else:
            print("  ❌ 응답 캡처 실패")

        print("\n=== 테스트 완료 ===")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
