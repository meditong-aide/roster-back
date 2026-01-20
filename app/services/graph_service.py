from agents.main_graph import GraphGenerate
from typing import Dict, Any, List, Optional


# class GraphService:
#     def __init__(self):
#         self._graph = GraphGenerate()

#     async def invoke(
#         self,
#         request: str | list[str],
#         schema: List[Dict[str, Any]],
#         case: Optional[list] = None,
#         year: int = None,
#         month: int = None,
#         allowed_shifts: str = "없음",
#         allowed_shift_map: Optional[Dict[str, str]] = None
#     ) -> List[Any]:
#         cleaned_request = self._clean_request(request)
        
#         print(f"[GraphService] 입력 파라미터 확인:")
#         print(f"  year: {year}, month: {month}")
#         print(f"  allowed_shifts: {allowed_shifts}")
#         print(f"  allowed_shift_map: {allowed_shift_map}")
#         print(f"  원본 request: {request}")
#         print(f"  정제된 request: '{cleaned_request}' (길이: {len(cleaned_request)})")

#         if not cleaned_request.strip():
#             print("[GraphService] request가 실질적으로 비어있음 → 그래프 스킵")
#             return [[], []]

#         input_state = {
#             "request": cleaned_request,
#             "schema": schema,
#             "case": case,
#             "year": year,
#             "month": month,
#             "allowed_shifts": allowed_shifts,
#             "allowed_shift_map": allowed_shift_map or {},
#         }

#         print("[GraphService] 그래프 실행 시작")
#         graph_output = await self._graph.ainvoke(input_state)

#         print(f"[GraphService] ainvoke 반환 타입: {type(graph_output)}")

#         # ★★★★★ 핵심: LangGraph의 AddableValuesDict를 일반 dict로 변환 ★★★★★
#         if not isinstance(graph_output, dict):
#             print("[GraphService] graph_output이 dict가 아님 → 강제 변환 시도")
#             try:
#                 graph_output = dict(graph_output)  # AddableValuesDict → dict
#                 print("[GraphService] dict 변환 성공")
#             except Exception as conv_err:
#                 print(f"[GraphService ERROR] dict 변환 실패: {conv_err}")
#                 graph_output = {"shift_results": [], "preference_results": []}

#         # 안전하게 키 추출
#         shift_results = graph_output.get('shift_results', [])
#         preference_results = graph_output.get('preference_results', [])

#         final_response = [shift_results, preference_results]

#         print("[GraphService] 그래프 실행 완료")
#         print(f"  shift_results 개수: {len(shift_results)}")
#         print(f"  preference_results 개수: {len(preference_results)}")

#         import pprint
#         pprint.pprint(final_response)
        
#         # shift_parsed 계산 (기존 코드 그대로, 하지만 shift_results가 str이 아닌지 확인)
#         shift_parsed = {}
#         if isinstance(shift_results, list):
#             for sublist in shift_results:
#                 if isinstance(sublist, list):
#                     for entry in sublist:
#                         if isinstance(entry, dict):
#                             for sr in entry.get('shift_result', []):
#                                 shift = sr.get('shift')
#                                 dates = sr.get('date', [])
#                                 scores = sr.get('score', [])
#                                 requests = sr.get('request', [])
                                
#                                 if not shift or not dates:
#                                     continue
                                
#                                 if shift not in shift_parsed:
#                                     shift_parsed[shift] = {}
                                
#                                 for i, day in enumerate(dates):
#                                     if i >= len(scores):
#                                         break
#                                     score = scores[i]
#                                     req = requests[i] if i < len(requests) else ''
                                    
#                                     current = shift_parsed[shift].get(day)
#                                     if current is None or score > current['score']:
#                                         shift_parsed[shift][day] = {
#                                             'score': score,
#                                             'request': req
#                                         }

#         print("[GraphService] 중복 제거 후 shift_parsed:")
#         pprint.pprint(shift_parsed)

#         return final_response

#     def _clean_request(self, request: str | list[str]) -> str:
#         """
#         request 값을 정제하여 실질적인 내용만 남김
#         - 리스트 → 공백/더미 값 필터링 후 join
#         - 문자열 → 공백 제거
#         - '기존 데이터에서 로드됨', '기존 데이터 업데이트' 같은 더미 제거
#         """
#         dummy_values = {
#             '기존 데이터에서 로드됨',
#             '기존 데이터 업데이트',
#             ''  # 빈 문자열
#         }

#         if isinstance(request, list):
#             # 공백 제거 + 더미 값 제외
#             filtered = [
#                 str(r).strip() 
#                 for r in request 
#                 if str(r).strip() and str(r).strip() not in dummy_values
#             ]
#             return '\n'.join(filtered) if filtered else ''
        
#         # 문자열인 경우
#         cleaned = str(request).strip()
#         if cleaned in dummy_values:
#             return ''
#         return cleaned

# graph_service = GraphService() 


class GraphService:
    def __init__(self):
        self._graph = GraphGenerate()

    async def invoke(
        self,
        **kwargs  # ← 모든 키워드 인자를 자유롭게 받도록 변경
    ) -> List[Any]:
        """
        그래프 실행 - wanted_service.py에서 넘어오는 모든 인자를 처리
        db와 group_id는 state에 무조건 포함
        """
        # 입력값 디버깅 출력
        print("[GraphService] invoke 호출됨 - 입력 kwargs:")
        for k, v in kwargs.items():
            if k in ["schema", "case"]:
                print(f"  {k}: (길이 {len(v)})")
            else:
                print(f"  {k}: {v}")

        # request 정제
        request = kwargs.get("request")
        cleaned_request = self._clean_request(request)

        print(f"[GraphService] 입력 파라미터 확인:")
        print(f"  year: {kwargs.get('year')}, month: {kwargs.get('month')}")
        print(f"  allowed_shifts: {kwargs.get('allowed_shifts', '없음')}")
        print(f"  allowed_shift_map: {kwargs.get('allowed_shift_map')}")
        print(f"  원본 request: {request}")
        print(f"  정제된 request: '{cleaned_request}' (길이: {len(cleaned_request)})")

        if not cleaned_request.strip():
            print("[GraphService] request가 실질적으로 비어있음 → 그래프 스킵")
            return [[], []]

        # input_state 구성 - 기존 키 + db, group_id 강제 포함
        input_state = {
            "request": cleaned_request,
            "schema": kwargs.get("schema", []),
            "case": kwargs.get("case", None),
            "year": kwargs.get("year"),
            "month": kwargs.get("month"),
            "allowed_shifts": kwargs.get("allowed_shifts", "없음"),
            "allowed_shift_map": kwargs.get("allowed_shift_map", {}),
            
            # ★ 핵심 수정: db와 group_id 무조건 state에 넣기
            "db": kwargs.get("db"),  # wanted_service에서 넘어온 db 세션
            "group_id": kwargs.get("group_id"),  # wanted_service에서 넘어온 group_id
        }

        # db/group_id 전달 확인 로그
        print(f"[GraphService] state에 db 포함 여부: {'있음' if input_state.get('db') is not None else '없음'}")
        print(f"[GraphService] state에 group_id 포함 여부: {input_state.get('group_id')}")

        print("[GraphService] 그래프 실행 시작")
        try:
            graph_output = await self._graph.ainvoke(input_state)
        except Exception as e:
            print(f"[GraphService] 그래프 실행 중 오류: {e}")
            traceback.print_exc()
            return [[], []]

        print(f"[GraphService] ainvoke 반환 타입: {type(graph_output)}")

        # LangGraph의 AddableValuesDict를 일반 dict로 안전하게 변환
        if not isinstance(graph_output, dict):
            print("[GraphService] graph_output이 dict가 아님 → 강제 변환 시도")
            try:
                graph_output = dict(graph_output)
                print("[GraphService] dict 변환 성공")
            except Exception as conv_err:
                print(f"[GraphService ERROR] dict 변환 실패: {conv_err}")
                graph_output = {"shift_results": [], "preference_results": []}

        # 안전하게 키 추출
        shift_results = graph_output.get('shift_results', [])
        preference_results = graph_output.get('preference_results', [])

        final_response = [shift_results, preference_results]

        print("[GraphService] 그래프 실행 완료")
        print(f"  shift_results 개수: {len(shift_results)}")
        print(f"  preference_results 개수: {len(preference_results)}")

        import pprint
        pprint.pprint(final_response)
        
        shift_parsed = {}
        print("[DEBUG] shift_results 원본 구조:")
        pprint.pprint(shift_results)  # ← 구조 확인용 로그 추가 (필수!)

        shift_parsed = {}
        print("[DEBUG] shift_results 원본 구조:")
        pprint.pprint(shift_results)

        shift_parsed = {}
        print("[DEBUG] shift_results 원본 구조:")
        pprint.pprint(shift_results)

        if isinstance(shift_results, list):
            # ★ 수정: shift_results가 단일 리스트 형태이므로 1중 루프만 사용
            for entry in shift_results:
                if isinstance(entry, dict):
                    shift = entry.get('shift')
                    dates = entry.get('date', [])
                    scores = entry.get('score', [])
                    requests = entry.get('request', [])

                    if not shift or not dates:
                        continue

                    if shift not in shift_parsed:
                        shift_parsed[shift] = {}

                    for i, day in enumerate(dates):
                        if i >= len(scores):
                            break
                        score = scores[i]
                        req = requests[i] if i < len(requests) else ''

                        # 중복 날짜면 마지막 값으로 덮어쓰기
                        shift_parsed[shift][day] = {
                            'score': score,
                            'request': req
                        }
                        print(f"[shift_parsed 추가] {shift} {day}일 score={score}, request={req}")

        print("[GraphService] 중복 제거 후 shift_parsed:")
        pprint.pprint(shift_parsed)

        return final_response

    def _clean_request(self, request: str | list[str]) -> str:
        """
        request 값을 정제하여 실질적인 내용만 남김
        - 리스트 → 공백/더미 값 필터링 후 join
        - 문자열 → 공백 제거
        - '기존 데이터에서 로드됨', '기존 데이터 업데이트' 같은 더미 제거
        """
        dummy_values = {
            '기존 데이터에서 로드됨',
            '기존 데이터 업데이트',
            ''  # 빈 문자열
        }

        if isinstance(request, list):
            # 공백 제거 + 더미 값 제외
            filtered = [
                str(r).strip() 
                for r in request 
                if str(r).strip() and str(r).strip() not in dummy_values
            ]
            return '\n'.join(filtered) if filtered else ''
        
        # 문자열인 경우
        cleaned = str(request).strip()
        if cleaned in dummy_values:
            return ''
        return cleaned


graph_service = GraphService()