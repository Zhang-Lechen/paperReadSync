from paperread.errors import AppError
from paperread.models import Book, utc_now


def confirm_book(book: Book, *, confirmed: bool, corresponding_edition: bool = False) -> Book:
    if not confirmed:
        raise AppError("BOOK_UNCONFIRMED", "需要用户确认书籍和具体版本")
    if not (book.translator or book.publisher or book.isbn or corresponding_edition):
        raise AppError("EDITION_UNKNOWN", "无法判断版本，请确认是否作为对应电子版")
    return book.model_copy(update={"confirmed_by_user": True, "confirmed_at": utc_now(),
                                   "corresponding_edition": corresponding_edition})
