from __future__ import annotations

from .auth import load_credentials
from .errors import AuthenticationRequired, RequestFailed, SchemaError, SetupRequired


class ClassroomClient:
    def __init__(self, service, account_key: str, *, max_pages: int = 100):
        self.service = service
        self.account_key = account_key
        self.max_pages = max(1, min(max_pages, 1000))

    @classmethod
    def connect(cls):
        try:
            import httplib2
            from google_auth_httplib2 import AuthorizedHttp
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise SetupRequired("Classroom dependencies are not installed") from exc
        credentials, account_key = load_credentials()
        service = build(
            "classroom", "v1", cache_discovery=False, static_discovery=True,
            http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=20)),
        )
        return cls(service, account_key)

    def _list(self, endpoint, key: str, fields: str, **params):
        rows = []
        token = None
        seen = set()
        for _ in range(self.max_pages):
            try:
                request = endpoint.list(
                    pageSize=100, pageToken=token,
                    fields=f"nextPageToken,{key}({fields})", **params,
                )
                if request.method != "GET":
                    raise RequestFailed("Only read requests are permitted")
                result = request.execute(num_retries=2)
            except RequestFailed:
                raise
            except Exception as exc:
                # Never let OAuth tokens or the requested URL reach stdout/logs.
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status == 401 or type(exc).__name__ == "RefreshError":
                    raise AuthenticationRequired("Classroom sign-in is required") from exc
                raise RequestFailed("Classroom GET request failed") from exc
            if (not isinstance(result, dict) or not isinstance(result.get(key, []), list)
                    or not set(result).issubset({key, "nextPageToken"})):
                raise SchemaError("Invalid Classroom list response")
            rows.extend(result.get(key, []))
            token = result.get("nextPageToken")
            if token is None or token == "":
                return rows
            if not isinstance(token, str) or token in seen:
                raise SchemaError("Invalid Classroom pagination")
            seen.add(token)
        raise SchemaError("Classroom pagination exceeded the safety limit")

    def courses(self):
        return self._list(self.service.courses(), "courses", "id,name,alternateLink",
                          studentId="me", courseStates=["ACTIVE"])

    def assignments(self, course_id):
        return self._list(
            self.service.courses().courseWork(), "courseWork",
            "id,courseId,title,description,creationTime,updateTime,dueDate,dueTime,alternateLink",
            courseId=course_id, courseWorkStates=["PUBLISHED"], orderBy="updateTime desc",
        )

    def submissions(self, course_id):
        return self._list(
            self.service.courses().courseWork().studentSubmissions(), "studentSubmissions",
            "courseId,courseWorkId,state,late,updateTime",
            courseId=course_id, courseWorkId="-", userId="me",
        )

    def announcements(self, course_id):
        return self._list(
            self.service.courses().announcements(), "announcements",
            "id,courseId,text,creationTime,updateTime,alternateLink",
            courseId=course_id, announcementStates=["PUBLISHED"], orderBy="updateTime desc",
        )

    def materials(self, course_id):
        return self._list(
            self.service.courses().courseWorkMaterials(), "courseWorkMaterial",
            "id,courseId,title,description,creationTime,updateTime,alternateLink",
            courseId=course_id, courseWorkMaterialStates=["PUBLISHED"], orderBy="updateTime desc",
        )
