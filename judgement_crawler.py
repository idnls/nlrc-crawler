"""
중앙노동위원회 사건 + 초심사건 단계별 테스트 v2
- 핵심: 초심보기 클릭 시 발생하는 /detail.do AJAX 요청을 직접 캡처
- page.on("request") 로 POST body까지 확인해서 정확한 필터 조건 파악
"""
import asyncio
import re
import html
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


def clean_text(text):
    if not text: return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def extract_committee_from_detail(soup):
    for tag in soup.find_all(['th', 'caption', 'h2', 'h3', 'h4']):
        text = tag.get_text(strip=True)
        if '노동위원회' in text and ('판정' in text or '결정' in text):
            match = re.match(r'(.+노동위원회)', text)
            if match:
                return match.group(1).strip()
    for tag in soup.find_all(['th', 'caption', 'h2', 'h3', 'h4']):
        text = tag.get_text(strip=True)
        if '노동위원회' in text:
            match = re.match(r'(.+노동위원회)', text)
            if match:
                return match.group(1).strip()
    return None


def extract_matter_and_summary(soup):
    matter_text = '상세 내용 없음'
    summary_text = '상세 내용 없음'
    matter_label = '판정사항'
    summary_label = '판정요지'
    for keyword in ['판정사항', '결정사항']:
        th = soup.find('th', string=re.compile(rf'^{keyword}$')) or soup.find('th', string=keyword)
        if th:
            matter_label = keyword
            td = th.find_next('td')
            if td:
                matter_text = clean_text(td.get_text(separator="\n"))
            break
    for keyword in ['판정요지', '결정요지']:
        th = soup.find('th', string=keyword)
        if not th:
            for t in soup.find_all('th'):
                if t.get_text(strip=True) == keyword:
                    th = t
                    break
        if th:
            summary_label = keyword
            td = th.find_next('td')
            if td:
                summary_text = clean_text(td.get_text(separator="\n"))
            break
    return matter_text, summary_text, matter_label, summary_label


async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # ── /detail.do 요청을 모두 로깅 ──
        captured_requests = []
        def on_request(req):
            if "detail.do" in req.url:
                body = req.post_data or ""
                captured_requests.append({'url': req.url, 'method': req.method, 'body': body})
                print(f"\n  🌐 [REQUEST] {req.method} {req.url}")
                print(f"      body = {body[:300]}")

        page.on("request", on_request)

        try:
            url = "https://nlrc.go.kr/nlrc/mainCase/judgment/search/index.do"
            print(f"[1] 접속: {url}")
            await page.goto(url, wait_until="networkidle", timeout=60000)

            await page.fill('#pQuery', '부해')
            await page.focus('#pQuery')
            await page.keyboard.press('Enter')
            try:
                async with page.expect_response(
                    lambda res: "/list.do" in res.url and res.status == 200, timeout=30000
                ) as ri:
                    await page.click('.btnSearch')
                    list_content = await (await ri.value).text()
            except Exception as e:
                print(f"  ⚠️ list 캡처 실패: {e}")
                list_content = await page.content()

            soup = BeautifulSoup(list_content, 'html.parser')
            dl_list = soup.find_all('dl', class_='C_Cts')
            print(f"[2] 검색결과: {len(dl_list)}건")

            # 중앙노동위원회 사건 1건
            target = None
            for dl in dl_list:
                dt = dl.find('dt', class_='tit')
                if not dt: continue
                a = dt.find('a')
                if not a: continue
                strong = a.find('strong')
                if not strong or '중앙' not in strong.get_text(): continue
                spans = a.find_all('span')
                if not spans: continue
                target = {
                    'committee': clean_text(strong.get_text()),
                    'case_number': clean_text(spans[0].get_text()),
                }
                break

            if not target:
                print("❌ 중앙노동위원회 사건 없음")
                return

            print(f"\n[3] 테스트 대상: {target['case_number']} / {target['committee']}")

            # ── 재심 팝업 열기 ──
            selector = f'a[data-k2="{target["case_number"]}"]'
            captured_requests.clear()
            print(f"\n[4] 재심 클릭 (expect_response)...")
            async with page.expect_response(
                lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000
            ) as ri:
                await page.click(selector, force=True)
                detail_content = await (await ri.value).text()

            print(f"[4] 캡처 완료 ({len(detail_content)}자), 요청 {len(captured_requests)}건")

            detail_soup = BeautifulSoup(detail_content, 'html.parser')
            medi = detail_soup.find('input', {'id': 'medi_numb'}) or detail_soup.find('input', {'name': 'medi_numb'})
            even = detail_soup.find('input', {'id': 'even_numb'}) or detail_soup.find('input', {'name': 'even_numb'})
            gubn = detail_soup.find('input', {'id': 'even_gubn'}) or detail_soup.find('input', {'name': 'even_gubn'})
            medi_numb = medi.get('value', '').strip() if medi else '없음'
            even_numb = even.get('value', '').strip() if even else '없음'
            gubn_val  = gubn.get('value', '').strip() if gubn else '없음'

            print(f"[5] 재심 AJAX: even_numb={even_numb}, medi_numb={medi_numb}, even_gubn={gubn_val}")

            if not medi_numb or medi_numb == even_numb:
                print("[X] 초심사건 없음 — 종료")
                return

            # 혹시 이미 더 요청이 왔는지 잠깐 대기
            await asyncio.sleep(1)
            print(f"[5b] 1초 대기 후 추가 /detail.do 요청 수: {len(captured_requests)}")

            # ── 초심보기 클릭 (expect_response 방식) ──
            print(f"\n[6] 초심보기 버튼 클릭 (expect_response)...")
            captured_requests.clear()

            try:
                async with page.expect_response(
                    lambda res: "/detail.do" in res.url and res.status == 200,
                    timeout=10000
                ) as ri2:
                    clicked = await page.evaluate("""
                        () => {
                            const btn = Array.from(document.querySelectorAll('button')).find(
                                b => b.title === '초심보기' ||
                                     (b.textContent && b.textContent.trim().includes('초심보기'))
                            );
                            if (btn) { btn.click(); return true; }
                            return false;
                        }
                    """)
                    print(f"    버튼 클릭: {clicked}")
                    if not clicked:
                        print("❌ 초심보기 버튼 없음")
                        return

                initial_content = await (await ri2.value).text()
                print(f"[6] 초심 응답 캡처 완료 ({len(initial_content)}자)")
                print(f"[6] 이때 요청 {len(captured_requests)}건:")
                for r in captured_requests:
                    print(f"    body={r['body'][:300]}")

                init_soup = BeautifulSoup(initial_content, 'html.parser')
                m = init_soup.find('input', {'id': 'medi_numb'}) or init_soup.find('input', {'name': 'medi_numb'})
                e = init_soup.find('input', {'id': 'even_numb'}) or init_soup.find('input', {'name': 'even_numb'})
                g = init_soup.find('input', {'id': 'even_gubn'}) or init_soup.find('input', {'name': 'even_gubn'})
                print(f"\n[7] 초심 AJAX hidden inputs:")
                print(f"    even_numb={e.get('value') if e else '없음'}")
                print(f"    medi_numb={m.get('value') if m else '없음'}")
                print(f"    even_gubn={g.get('value') if g else '없음'}")

                init_committee = extract_committee_from_detail(init_soup)
                init_matter, _, init_matter_label, _ = extract_matter_and_summary(init_soup)
                print(f"\n[8] 최종 결과:")
                print(f"    초심 위원회  : {init_committee}")
                print(f"    [{init_matter_label}]: {init_matter[:200]}...")

            except Exception as e:
                print(f"[6] expect_response 실패: {e}")
                print(f"    이 시점까지 요청 {len(captured_requests)}건:")
                for r in captured_requests:
                    print(f"    body={r['body'][:300]}")

            print("\n✅ 테스트 완료")

        except Exception as e:
            import traceback
            print(f"\n❌ 예외: {e}")
            traceback.print_exc()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(test())
