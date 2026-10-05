import createClient from 'openapi-fetch';
import type { paths } from './schema';

export type Fetch = typeof globalThis.fetch;

export function createApiClient(fetch: Fetch) {
	return createClient<paths>({
		baseUrl: 'https://aws.naroah.top/auto-course/',
		fetch
	});
}
