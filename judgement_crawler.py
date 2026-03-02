"""
중앙노동위원회 사건 1건 + 초심사건 단계별 테스트 스크립트
각 단계에서 무엇이 있는지 상세 출력
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

        try:
            url = "https://nlrc.go.kr/nlrc/mainCase/judgment/search/index.do"
            print(f"[1] 접속: {url}")
            await page.goto(url, wait_until="networkidle", timeout=60000)

            # ── 검색 ──
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
                print(f"  ⚠️ list.do 캡처 실패: {e}")
                list_content = await page.content()

            soup = BeautifulSoup(list_content, 'html.parser')
            dl_list = soup.find_all('dl', class_='C_Cts')
            print(f"[2] 검색결과: {len(dl_list)}건")

            # ── 중앙노동위원회 사건 1건 추출 ──
            target = None
            for dl in dl_list:
                dt = dl.find('dt', class_='tit')
                if not dt:
                    continue
                a = dt.find('a')
                if not a:
                    continue
                strong = a.find('strong')
                if not strong or '중앙' not in strong.get_text():
                    continue
                spans = a.find_all('span')
                if not spans:
                    continue
                target = {
                    'committee': clean_text(strong.get_text()),
                    'case_number': clean_text(spans[0].get_text()),
                }
                break

            if not target:
                print("❌ 중앙노동위원회 사건 없음")
                return

            print(f"\n[3] 테스트 대상: {target['case_number']} / {target['committee']}")

            # ── 재심 상세 팝업 열기 ──
            selector = f'a[data-k2="{target["case_number"]}"]'
            print(f"[4] 클릭: {selector}")
            async with page.expect_response(
                lambda res: "/detail.do" in res.url and res.status == 200, timeout=15000
            ) as ri:
                await page.click(selector, force=True)
                detail_content = await (await ri.value).text()

            detail_soup = BeautifulSoup(detail_content, 'html.parser')

            # hidden inputs 확인
            medi = detail_soup.find('input', {'id': 'medi_numb'}) or detail_soup.find('input', {'name': 'medi_numb'})
            even = detail_soup.find('input', {'id': 'even_numb'}) or detail_soup.find('input', {'name': 'even_numb'})
            gubn = detail_soup.find('input', {'id': 'even_gubn'}) or detail_soup.find('input', {'name': 'even_gubn'})
            medi_numb = medi.get('value', '').strip() if medi else '없음'
            even_numb = even.get('value', '').strip() if even else '없음'
            even_gubn_val = gubn.get('value', '').strip() if gubn else '없음'

            print(f"[5] AJAX HTML hidden inputs:")
            print(f"    even_numb (재심사건번호) = {even_numb}")
            print(f"    medi_numb (초심사건번호) = {medi_numb}")
            print(f"    even_gubn               = {even_gubn_val}")

            matter, summary, matter_label, _ = extract_matter_and_summary(detail_soup)
            committee = extract_committee_from_detail(detail_soup)
            print(f"[6] 재심 위원회: {committee}")
            print(f"    [{matter_label}]: {matter[:100]}...")

            if not medi_numb or medi_numb == even_numb:
                print("[7] 초심사건 없음 (medi_numb 비어있거나 재심과 동일)")
                return

            print(f"\n[7] 초심보기 버튼 클릭 시도 (초심: {medi_numb})")

            # ── DOM에서 현재 even_gubn 확인 ──
            dom_gubn_before = await page.evaluate("""
                () => {
                    const el = document.querySelector('#even_gubn, input[name="even_gubn"]');
                    return el ? el.value : 'NOT FOUND';
                }
            """)
            print(f"[8] 클릭 전 DOM even_gubn = {dom_gubn_before}")

            # ── 버튼 찾기 ──
            btn_info = await page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    const found = btns.find(
                        b => b.title === '초심보기' ||
                             (b.textContent && b.textContent.trim().includes('초심보기'))
                    );
                    if (!found) return { found: false, count: btns.length };
                    return {
                        found: true,
                        title: found.title,
                        text: found.textContent.trim().substring(0, 30),
                        onclick: found.getAttribute('onclick') || '',
                        count: btns.length
                    };
                }
            """)
            print(f"[9] 버튼 탐색: {btn_info}")

            if not btn_info.get('found'):
                print("❌ 초심보기 버튼 없음!")
                return

            # ── 클릭 ──
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
            print(f"[10] 클릭 결과: {clicked}")

            # ── even_gubn 폴링 ──
            print("[11] even_gubn 폴링 시작 (최대 5초):")
            final_gubn = None
            for attempt in range(10):
                await asyncio.sleep(0.5)
                gubn_val = await page.evaluate("""
                    () => {
                        const el = document.querySelector('#even_gubn, input[name="even_gubn"]');
                        return el ? el.value : 'NOT FOUND';
                    }
                """)
                print(f"    [{attempt+1}] {(attempt+1)*0.5:.1f}s → even_gubn = {gubn_val}")
                final_gubn = gubn_val
                if gubn_val == 'JR':
                    print(f"    ✅ JR 감지!")
                    break

            # ── .layer-cont 읽기 ──
            print(f"\n[12] .layer-cont 읽기 (even_gubn 최종값: {final_gubn})")
            popup_html = await page.evaluate("""
                () => {
                    const layerCont = document.querySelector('.layer-cont');
                    if (layerCont) return { src: 'layer-cont', html: layerCont.outerHTML };
                    const layerWrap = document.querySelector('.layer-wrap');
                    if (layerWrap) return { src: 'layer-wrap', html: layerWrap.outerHTML };
                    return null;
                }
            """)

            if not popup_html:
                print("❌ popup HTML 없음!")
                return

            print(f"    소스: {popup_html['src']}, HTML 길이: {len(popup_html['html'])}자")

            init_soup = BeautifulSoup(popup_html['html'], 'html.parser')

            # hidden inputs 재확인
            medi2 = init_soup.find('input', {'id': 'medi_numb'}) or init_soup.find('input', {'name': 'medi_numb'})
            even2 = init_soup.find('input', {'id': 'even_numb'}) or init_soup.find('input', {'name': 'even_numb'})
            gubn2 = init_soup.find('input', {'id': 'even_gubn'}) or init_soup.find('input', {'name': 'even_gubn'})
            print(f"\n[13] 초심 팝업 hidden inputs:")
            print(f"    even_numb = {even2.get('value') if even2 else '없음'}")
            print(f"    medi_numb = {medi2.get('value') if medi2 else '없음'}")
            print(f"    even_gubn = {gubn2.get('value') if gubn2 else '없음'}")

            init_committee = extract_committee_from_detail(init_soup)
            init_matter, init_summary, init_matter_label, _ = extract_matter_and_summary(init_soup)

            print(f"\n[14] 결과:")
            print(f"    초심 위원회: {init_committee}")
            print(f"    [{init_matter_label}]: {init_matter[:150]}...")

            print("\n✅ 테스트 완료")

        except Exception as e:
            import traceback
            print(f"\n❌ 예외 발생: {e}")
            traceback.print_exc()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(test())
