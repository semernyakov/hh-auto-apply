"""Регрессионные тесты для детекторов из auto_reply.py.

Запуск:
    python -m unittest tests.test_detectors    # без зависимостей
    pytest tests/test_detectors.py             # если установлен pytest

Все тяжёлые импорты (anthropic, playwright) замокированы в tests/conftest.py.
"""

from __future__ import annotations

import unittest

from tests import conftest  # noqa: F401 — побочные эффекты: подмена модулей

import auto_reply as ar  # noqa: E402


# -------- is_robot_questionnaire ------------------------------------------------


class RobotQuestionnaireTests(unittest.TestCase):
    def test_author_marker_robot(self) -> None:
        h = "[Робот-рекрутёр]\nЗдравствуйте!\n\n[Робот-рекрутёр]\nБыл ли у Вас опыт ctypes?"
        flag, reason = ar.is_robot_questionnaire(h)
        self.assertTrue(flag)
        self.assertTrue(reason.startswith("author_marker:"))

    def test_author_marker_hr_bot(self) -> None:
        flag, _ = ar.is_robot_questionnaire("[HR-бот компании]\nСколько лет опыта в Python?")
        self.assertTrue(flag)

    def test_author_marker_eng_bot(self) -> None:
        flag, _ = ar.is_robot_questionnaire("[Recruiting bot]\nВладеете ли вы Go?")
        self.assertTrue(flag)

    def test_pattern_series_two_in_a_row(self) -> None:
        h = (
            "[Иван Петров]\nБыл ли у Вас опыт разработки C-extension?\n\n"
            "[Я]\nДа.\n\n"
            "[Иван Петров]\nБыл ли опыт shared memory?"
        )
        flag, reason = ar.is_robot_questionnaire(h)
        self.assertTrue(flag)
        self.assertEqual(reason, "pattern:series")

    def test_live_hr_single_short_question_not_detected(self) -> None:
        flag, _ = ar.is_robot_questionnaire("[Работодатель]\nЗдравствуйте, можете рассказать о себе?")
        self.assertFalse(flag)

    def test_empty_history(self) -> None:
        self.assertEqual(ar.is_robot_questionnaire(""), (False, ""))

    def test_only_self_messages(self) -> None:
        flag, _ = ar.is_robot_questionnaire("[Я]\nДобрый день, готов к интервью.")
        self.assertFalse(flag)

    def test_pattern_requires_consecutive(self) -> None:
        # Только один шаблонный вопрос среди прочего → не анкета.
        h = (
            "[Анна]\nДавайте обсудим вашу мотивацию.\n\n"
            "[Я]\nКонечно.\n\n"
            "[Анна]\nКакой у вас опыт работы с Python?"
        )
        flag, _ = ar.is_robot_questionnaire(h)
        self.assertFalse(flag)


# -------- is_rejection ----------------------------------------------------------


class RejectionTests(unittest.TestCase):
    def test_direct_rejection(self) -> None:
        self.assertTrue(ar.is_rejection("[Работодатель]\nК сожалению, не подходите по опыту."))

    def test_polite_postponement(self) -> None:
        self.assertTrue(ar.is_rejection("[HR]\nСвяжемся с вами позже."))

    def test_last_msg_is_ours_ignored(self) -> None:
        h = "[Работодатель]\nК сожалению, не подходите.\n\n[Я]\nСпасибо за обратную связь."
        self.assertFalse(ar.is_rejection(h))

    def test_positive_is_not_rejection(self) -> None:
        self.assertFalse(ar.is_rejection("[HR]\nХотим пригласить вас на интервью."))

    def test_empty(self) -> None:
        self.assertFalse(ar.is_rejection(""))


# -------- detect_positive_signal ------------------------------------------------


class PositiveSignalTests(unittest.TestCase):
    def test_interview_invite(self) -> None:
        res = ar.detect_positive_signal("[HR]\nХотим пригласить вас на интервью завтра в 15:00.")
        self.assertIsNotNone(res)
        assert res is not None  # for type checkers
        self.assertEqual(res[0], "interview")

    def test_contact_request(self) -> None:
        res = ar.detect_positive_signal("[HR]\nНапишите мне в телеграм, обсудим детали.")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res[0], "contact_request")

    def test_under_review(self) -> None:
        res = ar.detect_positive_signal("[HR]\nВаше резюме передали нанимающему менеджеру.")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res[0], "under_review")

    def test_after_self_reply_no_signal(self) -> None:
        # Последний блок — наш ответ → новых сигналов не считаем.
        h = "[HR]\nХотим пригласить на интервью.\n\n[Я]\nГотов, удобно завтра."
        self.assertIsNone(ar.detect_positive_signal(h))

    def test_neutral_message(self) -> None:
        self.assertIsNone(ar.detect_positive_signal("[HR]\nКаков ваш опыт?"))


# -------- _strip_role_prefix + _looks_like_questionnaire_question ---------------


class HelperTests(unittest.TestCase):
    def test_strip_role_prefix(self) -> None:
        self.assertEqual(ar._strip_role_prefix("[HR]\ntext"), ("HR", "text"))
        self.assertEqual(ar._strip_role_prefix("no role"), ("", "no role"))

    def test_looks_like_questionnaire_question(self) -> None:
        self.assertTrue(ar._looks_like_questionnaire_question("Был ли у Вас опыт работы с Go?"))
        self.assertTrue(ar._looks_like_questionnaire_question("Сколько лет опыта в Python?"))
        self.assertFalse(ar._looks_like_questionnaire_question("Расскажите о себе подробнее"))
        self.assertFalse(ar._looks_like_questionnaire_question(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
