import { NextResponse, type NextRequest } from 'next/server';

/**
 * Optimistic auth gate: if there is no session cookie at all, send the visitor to /login.
 * The cookie is httpOnly and opaque; real validation happens on the API (401 -> client redirect).
 */
export function proxy(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  if (!req.cookies.has('lead_session')) {
    const url = req.nextUrl.clone();
    url.pathname = '/login';
    url.search = '';
    if (pathname !== '/' && pathname !== '/dashboard') url.searchParams.set('next', pathname + search);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  // Everything except login, design (dev QA), API/health rewrites and Next internals/static files.
  matcher: ['/((?!login|design|api|healthz|_next|favicon.ico|.*\\..*).*)'],
};
