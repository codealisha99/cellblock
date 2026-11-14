"""Domain errors. The API layer maps each one to an HTTP status."""


class CellblockError(Exception):
    status_code = 500


class BadRequest(CellblockError):
    status_code = 400


class Unauthorized(CellblockError):
    status_code = 401


class NotFound(CellblockError):
    status_code = 404


class Conflict(CellblockError):
    status_code = 409


class QuotaExceeded(CellblockError):
    status_code = 429


class ImageNotAllowed(CellblockError):
    status_code = 422
