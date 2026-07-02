# data.go.kr External Adapter Backlog

This backlog is generated from route-disposition evidence and includes only `adapter_candidate` routes. Dead-route and transient-failure routes remain evidence, not adapter implementation work.

- Generated at: `2026-07-02T16:57:24Z`
- Candidate hosts: `10`
- Candidate operations: `24`
- Candidate APIs: `13`
- Candidate institutions: `10`
- Raw missing adapter operations: `53`
- Dead-route candidates excluded: `14`
- Transient failures excluded: `15`
- Unclassified missing routes: `0`

## Candidate Hosts

| Host | Ops | APIs | Institutions | Status |
| --- | --- | --- | --- | --- |
| `www.nongsaro.go.kr` | 4 | 2 | 농촌진흥청 | adapter_not_registered |
| `data.gwanak.go.kr` | 3 | 2 | 서울특별시 관악구 | adapter_not_registered |
| `data.mafra.go.kr` | 3 | 2 | 농림수산식품교육문화정보원, 농림축산식품부 | adapter_not_registered |
| `www.garak.co.kr` | 3 | 1 | 서울특별시농수산식품공사 | adapter_not_registered |
| `www.work24.go.kr` | 3 | 1 | 한국고용정보원 | adapter_not_registered |
| `data.seoul.go.kr` | 2 | 1 | 서울교통공사 | adapter_not_registered |
| `www.culture.go.kr` | 2 | 1 | 한국체육산업개발주식회사 | adapter_not_registered |
| `www.happysd.or.kr` | 2 | 2 | 서울특별시성동구도시관리공단 | adapter_not_registered |
| `ncpms.rda.go.kr` | 1 | 1 | 농촌진흥청 | adapter_not_registered |
| `search.i815.or.kr` | 1 | 1 | 독립기념관 | adapter_not_registered |

## Sample Candidate Routes

| Host | API ID | Institution | Title | Operation |
| --- | --- | --- | --- | --- |
| `data.gwanak.go.kr` | 15007009 | 서울특별시 관악구 | 서울특별시 관악구_공중위생업소 현황 | 서울특별시 관악구_공중위생업소 현황_20220624 외부 링크 1 |
| `data.gwanak.go.kr` | 15007009 | 서울특별시 관악구 | 서울특별시 관악구_공중위생업소 현황 | 서울특별시 관악구_공중위생업소 현황_20220624 외부 링크 2 |
| `data.gwanak.go.kr` | 15009684 | 서울특별시 관악구 | 서울특별시 관악구_공공시설개방정보 | 서울특별시 관악구_공공시설개방정보_20220624 |
| `data.mafra.go.kr` | 15002220 | 농림축산식품부 | 농림축산식품부_유기농업자재 현황 | 유기농업자재 현황_20210402 외부 링크 1 |
| `data.mafra.go.kr` | 15002220 | 농림축산식품부 | 농림축산식품부_유기농업자재 현황 | 유기농업자재 현황_20210402 외부 링크 2 |
| `data.mafra.go.kr` | 15008409 | 농림수산식품교육문화정보원 | 농림수산식품교육문화정보원_중국 도매시장 가격정보 | 중국농산물 가격정보 |
| `data.seoul.go.kr` | 15003164 | 서울교통공사 | 서울교통공사_역사 건축 현황 | 서울교통공사_역사 건축 현황_20240331 외부 링크 1 |
| `data.seoul.go.kr` | 15003164 | 서울교통공사 | 서울교통공사_역사 건축 현황 | 서울교통공사_역사 건축 현황_20240331 외부 링크 2 |
| `ncpms.rda.go.kr` | 15002034 | 농촌진흥청 | 농촌진흥청_국가농작물병해충도감정보 | 농촌진흥청_국가농작물병해충도감정보_20240222 외부 링크 1 |
| `search.i815.or.kr` | 15006273 | 독립기념관 | 독립기념관_한국독립운동사 소장자료 정보 DB | 독립기념관_한국독립운동사 소장자료 정보 DB_20200703 |
| `www.culture.go.kr` | 15008517 | 한국체육산업개발주식회사 | 한국체육산업개발주식회사_올림픽공원 도서정보 | 한국체육산업개발주식회사_올림픽공원 도서정보_20230918 외부 링크 1 |
| `www.culture.go.kr` | 15008517 | 한국체육산업개발주식회사 | 한국체육산업개발주식회사_올림픽공원 도서정보 | 한국체육산업개발주식회사_올림픽공원 도서정보_20230918 외부 링크 2 |
| `www.garak.co.kr` | 15004517 | 서울특별시농수산식품공사 | 서울특별시농수산식품공사_주요 품목 가격 | 서울특별시농수산식품공사_주요 품목 가격_20210113 외부 링크 1 |
| `www.garak.co.kr` | 15004517 | 서울특별시농수산식품공사 | 서울특별시농수산식품공사_주요 품목 가격 | 서울특별시농수산식품공사_주요 품목 가격_20210113 외부 링크 2 |
| `www.garak.co.kr` | 15004517 | 서울특별시농수산식품공사 | 서울특별시농수산식품공사_주요 품목 가격 | 서울특별시농수산식품공사_주요 품목 가격_20210113 외부 링크 3 |
| `www.happysd.or.kr` | 15007444 | 서울특별시성동구도시관리공단 | 서울특별시성동구도시관리공단_체육시설 강좌정보 조회 | 서울특별시성동구도시관리공단_체육시설 강좌정보 조회_20220803 |
| `www.happysd.or.kr` | 15007467 | 서울특별시성동구도시관리공단 | 서울특별시성동구도시관리공단_체육시설 대관정보 조회 | 서울특별시성동구도시관리공단_체육시설 대관정보 조회_20220804 |
| `www.nongsaro.go.kr` | 15002034 | 농촌진흥청 | 농촌진흥청_국가농작물병해충도감정보 | 농촌진흥청_국가농작물병해충도감정보_20240222 외부 링크 2 |
| `www.nongsaro.go.kr` | 15005257 | 농촌진흥청 | 농촌진흥청_농촌교육농장 정보 | 농촌진흥청_농촌교육농장 정보_20171211 외부 링크 1 |
| `www.nongsaro.go.kr` | 15005257 | 농촌진흥청 | 농촌진흥청_농촌교육농장 정보 | 농촌진흥청_농촌교육농장 정보_20171211 외부 링크 2 |
| `www.nongsaro.go.kr` | 15005257 | 농촌진흥청 | 농촌진흥청_농촌교육농장 정보 | 농촌진흥청_농촌교육농장 정보_20171211 외부 링크 3 |
| `www.work24.go.kr` | 15003549 | 한국고용정보원 | 한국고용정보원_워크넷_학과정보_학과목록 및 일반학과 상세, 이색학과 상세 | 한국고용정보원_워크넷_학과정보_학과목록 및 일반학과 상세, 이색학과 상세_20210520 외부 링크 1 |
| `www.work24.go.kr` | 15003549 | 한국고용정보원 | 한국고용정보원_워크넷_학과정보_학과목록 및 일반학과 상세, 이색학과 상세 | 한국고용정보원_워크넷_학과정보_학과목록 및 일반학과 상세, 이색학과 상세_20210520 외부 링크 2 |
| `www.work24.go.kr` | 15003549 | 한국고용정보원 | 한국고용정보원_워크넷_학과정보_학과목록 및 일반학과 상세, 이색학과 상세 | 한국고용정보원_워크넷_학과정보_학과목록 및 일반학과 상세, 이색학과 상세_20210520 외부 링크 3 |

The full machine-readable backlog is `reports/data-go-kr/external-adapter-backlog.json`.
