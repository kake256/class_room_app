from grader.fetch import list_teacher_courses, teacher_course


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeCourses:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["pageToken"] is None:
            return FakeRequest({
                "courses": [{"id": "1", "name": "A", "courseState": "ACTIVE"}],
                "nextPageToken": "next",
            })
        return FakeRequest({
            "courses": [{"id": "2", "name": "B", "courseState": "ACTIVE"}],
        })


class FakeClassroom:
    def __init__(self):
        self.resource = FakeCourses()

    def courses(self):
        return self.resource


def test_teacher_courses_are_active_minimal_and_paginated():
    service = FakeClassroom()
    assert [course["id"] for course in list_teacher_courses(service)] == ["1", "2"]
    assert len(service.resource.calls) == 2
    first = service.resource.calls[0]
    assert first["teacherId"] == "me" and first["courseStates"] == ["ACTIVE"]
    assert first["fields"] == "courses(id,name,section,courseState),nextPageToken"
    assert service.resource.calls[1]["pageToken"] == "next"


def test_teacher_course_rejects_id_not_in_teachers_list():
    service = FakeClassroom()
    assert teacher_course(service, "999") is None
