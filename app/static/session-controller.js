(function (root, factory) {
  const createSessionController = factory();
  if (typeof module === 'object' && module.exports) module.exports = createSessionController;
  if (root) root.createSessionController = createSessionController;
})(typeof globalThis === 'undefined' ? this : globalThis, function () {
  return function createSessionController({storage, clearHistory}) {
    let sessionId = storage.getItem('chat_session_id');
    return {
      get sessionId() {
        return sessionId;
      },
      async replaceSession(createSession) {
        const session = await createSession();
        sessionId = session.id;
        storage.setItem('chat_session_id', sessionId);
        clearHistory();
        return session;
      },
      clearSession() {
        sessionId = null;
        storage.removeItem('chat_session_id');
      },
    };
  };
});
