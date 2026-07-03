# data.go.kr Operation Materialization Plan

This plan is generated from `reports/data-go-kr/coverage-backlog.json` and turns APIs without operation mappings into bounded institution work queues. It is separate from runtime evidence reactivation: these APIs need operation metadata materialized before they can enter verification batches.

- Generated at: `2026-07-03T16:12:05Z`
- Institutions: `411`
- APIs: `12060`
- APIs with operation mapping: `9357` (`77.6%`)
- APIs without operation mapping: `2703`
- Planned institutions: `10`
- Planned APIs: `546`
- First queue: `성평등가족부`
- Batch size: `100`

## Planned Institution Batches

| Rank | Institution | APIs | Covered APIs | Uncovered APIs | API Coverage | Planned APIs | Remaining After Batch |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 성평등가족부 | 273 | 272 | 1 | 99.6% | 1 | 0 |
| 2 | 공정거래위원회 | 250 | 216 | 34 | 86.4% | 34 | 0 |
| 3 | 한국산업인력공단 | 230 | 228 | 2 | 99.1% | 2 | 0 |
| 4 | 국립암센터 | 212 | 204 | 8 | 96.2% | 8 | 0 |
| 5 | 법제처 | 203 | 5 | 198 | 2.5% | 100 | 98 |
| 6 | 경기도 광명시 | 197 | 0 | 197 | 0.0% | 100 | 97 |
| 7 | 해양수산부 | 173 | 172 | 1 | 99.4% | 1 | 0 |
| 8 | 제주특별자치도 | 171 | 18 | 153 | 10.5% | 100 | 53 |
| 9 | 농촌진흥청 | 136 | 14 | 122 | 10.3% | 100 | 22 |
| 10 | 대전광역시 서구 | 125 | 0 | 125 | 0.0% | 100 | 25 |

## Sample APIs To Materialize

| Institution | API ID | Title | Category | Format | Dev Approval | Prod Approval | Updated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 성평등가족부 | 3072018 | 성평등가족부_성범죄자 지역별 통계 | 사회복지 | XML | 자동승인 | 자동승인 | 2025-06-17 |
| 공정거래위원회 | 15144425 | 공정거래위원회_페어데이터_가맹정보 자연어 기반 질의 학습데이터 목록 제공서비스 | 산업고용 | JSON | 자동승인 | 심의승인 | 2025-08-04 |
| 공정거래위원회 | 15143710 | 공정거래위원회_페어데이터_브랜드별 가맹점/직영점 집계 및 가맹사업자 평균매출 학습데이터 제공서비스 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-07-01 |
| 공정거래위원회 | 15143711 | 공정거래위원회_페어데이터_가맹사업자 부담금 및 인테리어금액 학습데이터 제공서비스 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-06-24 |
| 공정거래위원회 | 15143709 | 공정거래위원회_페어데이터_브랜드 지역별 가맹점 평균 매출액 제공서비스 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-06-24 |
| 공정거래위원회 | 15143704 | 공정거래위원회_페어데이터_해외 가맹본부 주소 및 브랜드수/계열회사수 학습데이터 제공서비스 | 산업고용 | JSON+XML | 자동승인 | 심의승인 | 2025-06-24 |
| 한국산업인력공단 | 3045136 | [산업인력] 해외취업 통계정보 | 산업고용 | XML | 자동승인 | 자동승인 | 2025-06-11 |
| 한국산업인력공단 | 3038249 | 한국산업인력공단_해외진출정보 | 산업고용 | XML | 자동승인 | 자동승인 | 2018-05-02 |

## Regeneration Loop

After materializing operation mappings for a batch, regenerate the coverage backlog, this plan, the institution API overview, and the institution runtime plan. APIs that gain operations should leave this plan and enter runtime reactivation if they still lack verification evidence. The per-institution batch JSON files under `reports/data-go-kr/operation-materialization-batches/` are the handoff unit for mapping work.
