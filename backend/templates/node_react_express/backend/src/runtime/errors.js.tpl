function jsonErrorHandler(error, _request, response, _next) {
  const status = Number.isInteger(error.status) ? error.status : 500;
  response.status(status).json({ error: status === 500 ? 'Internal server error' : error.message });
}

module.exports = { jsonErrorHandler };
