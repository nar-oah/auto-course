<script lang="ts">
	import { createApiClient, type Fetch } from './api/client';

	let { fetch }: { fetch: Fetch } = $props();
	const client = $derived(createApiClient(fetch));

	let username = $state('');
	let password = $state('');
	let pending = $state(false);
	let submitted = $state(false);
	let error = $state('');

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (pending) return;

		pending = true;
		submitted = false;
		error = '';

		try {
			const { response } = await client.POST('/start', {
				body: { username, password }
			});

			if (response.status === 202) {
				submitted = true;
				password = '';
			} else {
				error = '请求提交失败，请稍后重试。';
			}
		} catch {
			error = '无法提交请求，请检查网络后重试。';
		} finally {
			pending = false;
		}
	}
</script>

<main>
	<section aria-labelledby="title">
		<h1 id="title">自动学习</h1>
		<form method="post" autocomplete="off" onsubmit={submit} aria-busy={pending}>
			<label for="username">账号</label>
			<input
				id="username"
				name="username"
				type="text"
				bind:value={username}
				autocomplete="off"
				autocapitalize="none"
				spellcheck={false}
				required
				disabled={pending}
			/>

			<label for="password">密码</label>
			<input
				id="password"
				name="password"
				type="password"
				bind:value={password}
				autocomplete="off"
				required
				disabled={pending}
			/>

			<button type="submit" disabled={pending}>{pending ? '提交中…' : '提交'}</button>
		</form>

		{#if submitted}
			<p class="success" role="status">请求已经提交。</p>
		{:else if error}
			<p class="error" role="alert">{error}</p>
		{/if}
	</section>
</main>

<style>
	main {
		display: grid;
		min-height: 100svh;
		place-items: center;
		padding: 1.5rem;
		box-sizing: border-box;
		background: #f4f6fa;
		color: #172033;
		font-family: system-ui, sans-serif;
	}

	section {
		width: 100%;
		max-width: 24rem;
		padding: 2rem;
		box-sizing: border-box;
		border: 1px solid #e1e5ed;
		border-radius: 1rem;
		background: white;
		box-shadow: 0 0.5rem 2rem #17203308;
	}

	h1 {
		margin: 0 0 1.75rem;
		font-size: 1.5rem;
		font-weight: 650;
	}

	form {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	label {
		font-size: 0.875rem;
		font-weight: 550;
	}

	input,
	button {
		min-height: 2.75rem;
		padding: 0.625rem 0.75rem;
		box-sizing: border-box;
		border-radius: 0.5rem;
		font: inherit;
	}

	input {
		margin-bottom: 0.75rem;
		border: 1px solid #cbd3df;
		background: white;
		color: inherit;
	}

	button {
		margin-top: 0.25rem;
		border: 1px solid #2563eb;
		background: #2563eb;
		color: white;
		font-weight: 600;
		cursor: pointer;
	}

	input:focus-visible,
	button:focus-visible {
		outline: 3px solid #93b4fc;
		outline-offset: 2px;
	}

	button:hover:enabled {
		background: #1d4ed8;
	}

	:disabled {
		opacity: 0.65;
		cursor: wait;
	}

	p {
		margin: 1rem 0 0;
		font-size: 0.875rem;
		line-height: 1.5;
	}

	.success {
		color: #167242;
	}

	.error {
		color: #b42318;
	}
</style>
