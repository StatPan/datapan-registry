# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-02T22:37:33Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `7789` (`64.6%`)
- APIs without operation mapping: `4271`
- Planned institutions: `10`
- Planned APIs: `645`
- First queue: `행정안전부`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 행정안전부 | 1252 | 988 | 264 | 78.9% | 100 | 164 |
| 2 | 경기도 | 840 | 199 | 641 | 23.7% | 100 | 541 |
| 3 | 국토교통부 | 393 | 127 | 266 | 32.3% | 100 | 166 |
| 4 | 식품의약품안전처 | 392 | 272 | 120 | 69.4% | 100 | 20 |
| 5 | 국회 국회사무처 | 277 | 0 | 277 | 0.0% | 100 | 177 |
| 6 | 성평등가족부 | 273 | 272 | 1 | 99.6% | 1 | 0 |
| 7 | 공정거래위원회 | 250 | 216 | 34 | 86.4% | 34 | 0 |
| 8 | 한국산업인력공단 | 230 | 228 | 2 | 99.1% | 2 | 0 |
| 9 | 국립암센터 | 212 | 204 | 8 | 96.2% | 8 | 0 |
| 10 | 법제처 | 203 | 5 | 198 | 2.5% | 100 | 98 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 행정안전부 | 15149535 | 행정안전부_생활안전지도 교육환경보호구역 | 국토관리 | WMS | 자동승인 | 심의승인 | 2025-11-19 |
| 행정안전부 | 15101888 | 행정안전부_생활안전지도 유아시설 | 교육 | WMS | 자동승인 | 심의승인 | 2025-11-19 |
| 행정안전부 | 15151925 | 행정안전부_동물의약품별잔류허용기준 | 식품건강 | JSON | 자동승인 | 심의승인 | 2025-11-11 |
| 행정안전부 | 15150624 | 행정안전부_행정처분결과_식품접객업 | 식품건강 | JSON | 자동승인 | 심의승인 | 2025-09-25 |
| 행정안전부 | 15150623 | 행정안전부_행정처분결과_수입식품업 | 식품건강 | JSON | 자동승인 | 심의승인 | 2025-09-25 |
| 경기도 | 15059050 | 경기도_노인 휴양ㆍ복합복지시설 현황 | 사회복지 | XML | 자동승인 | 자동승인 | 2025-09-25 |
| 경기도 | 15058592 | 경기도_재가 노인 복지시설 현황 | 사회복지 | XML | 자동승인 | 자동승인 | 2025-09-25 |
| 경기도 | 15058470 | 경기도_급식비 집행 실적_부담 주체 현황 | 교육 | XML | 자동승인 | 자동승인 | 2025-09-25 |
| 경기도 | 15058189 | 경기도_건설하도급 부조리 신고센터 현황 | 공공행정 | XML | 자동승인 | 자동승인 | 2025-09-25 |
| 경기도 | 15057837 | 경기도_노인여가복지시설(경로당) 현황 | 사회복지 | XML | 자동승인 | 자동승인 | 2025-09-25 |
| 국토교통부 | 15067164 | 국토교통부_건설보고서 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15067161 | 국토교통부_건설 기술사례정보 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15061112 | 국토교통부_건설공사 원가절감사례 목록 조회 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15061101 | 국토교통부_설계VE 상세 VE제안목록 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |
| 국토교통부 | 15061098 | 국토교통부_품질검사전문기관 상세정보 | 교통물류 | JSON+XML | 자동승인 | 심의승인 | 2025-11-18 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
