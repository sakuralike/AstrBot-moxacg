<?php

namespace AstrBotZibll {
    // Only identity/CSRF and theme services are doubles; REST and wpdb are real.
    function current_user_can($capability)
    {
        return $capability === 'manage_options' && $GLOBALS['az_test_admin'];
    }

    function wp_verify_nonce($nonce, $action)
    {
        return $nonce === 'test-csrf' && $action === 'astrbot_zibll_settings';
    }

    function wp_nonce_field($action)
    {
        echo '<input type="hidden" name="_wpnonce" value="test-csrf">';
    }

    function wp_get_theme()
    {
        return new class {
            public function get_template()
            {
                return $GLOBALS['az_test_theme'];
            }
        };
    }

    function function_exists($name)
    {
        return !in_array($name, $GLOBALS['az_test_missing_functions'], true);
    }

    function _pz($name, $default = false)
    {
        return $GLOBALS['az_test_theme_options'][$name] ?? $default;
    }
}

namespace {
    if (PHP_SAPI !== 'cli') {
        exit(1);
    }
    $root = getenv('AZ_WP_ROOT');
    if (!$root || !is_file($root . '/wp-load.php')) {
        throw new \RuntimeException('Set AZ_WP_ROOT to an isolated WordPress test source.');
    }
    define('ABSPATH', rtrim($root, '/') . '/');
    define('SHORTINIT', true);
    define('DONOTCACHEPAGE', true);
    define('WP_DISABLE_FATAL_ERROR_HANDLER', true);
    require_once ABSPATH . 'wp-includes/plugin.php';
    add_filter('enable_loading_advanced_cache_dropin', static fn() => false);
    $_SERVER['HTTP_HOST'] = 'localhost:18797';
    $_SERVER['HTTPS'] = 'on';
    require ABSPATH . 'wp-load.php';
    if (wp_using_ext_object_cache()) {
        throw new \RuntimeException('Refusing to test with a shared external object cache.');
    }
    wp_plugin_directory_constants();
    $GLOBALS['wp_plugin_paths'] = array();
    $GLOBALS['l10n'] = array();
    require_once ABSPATH . 'wp-includes/l10n.php';
    require_once ABSPATH . 'wp-includes/link-template.php';
    require_once ABSPATH . 'wp-includes/general-template.php';
    require_once ABSPATH . 'wp-includes/theme.php';
    require_once ABSPATH . 'wp-includes/class-wp-walker.php';
    require_once ABSPATH . 'wp-includes/kses.php';
    require_once ABSPATH . 'wp-includes/http.php';
    require_once ABSPATH . 'wp-includes/class-wp-http-response.php';
    require_once ABSPATH . 'wp-includes/rest-api/class-wp-rest-request.php';
    require_once ABSPATH . 'wp-includes/rest-api/class-wp-rest-response.php';
    require_once ABSPATH . 'wp-includes/rest-api/class-wp-rest-server.php';
    require_once ABSPATH . 'wp-includes/rest-api.php';
    require dirname(__DIR__) . '/astrbot-zibll.php';

    if (($argv[1] ?? '') === '--nonce-worker') {
        $prefix = $argv[2] ?? '';
        if (!preg_match('/\Aazt_[0-9a-f]{12}_\z/', $prefix)) {
            throw new \RuntimeException('Invalid test prefix.');
        }
        $wpdb->set_prefix($prefix);
        while (microtime(true) < (float) $argv[5]) {
            usleep(1000);
        }
        $result = \AstrBotZibll\consume_nonce('test-client', 'test-key', $argv[3], (int) $argv[4]);
        echo $result === true ? 'ok' : $result->get_error_code();
        exit;
    }

    $checks = 0;
    function check($condition, $label)
    {
        global $checks;
        if (!$condition) {
            throw new \RuntimeException($label);
        }
        ++$checks;
    }

    function error_is($result, $code, $status)
    {
        return is_wp_error($result) && $result->get_error_code() === $code
            && $result->get_error_data()['status'] === $status;
    }

    function request_for($options, $timestamp = null, $nonce = null)
    {
        $request = new \WP_REST_Request('GET', '/astrbot-zibll/v1/capabilities');
        $request->set_body('');
        $timestamp = (string) ($timestamp ?? time());
        $nonce = $nonce ?? bin2hex(random_bytes(16));
        foreach (array(
            'x-az-client' => $options['client_id'],
            'x-az-key' => $options['key_id'],
            'x-az-time' => $timestamp,
            'x-az-nonce' => $nonce,
            'x-az-signature' => \AstrBotZibll\signature(
                $options['client_id'], $options['key_id'], $options['secret'],
                'GET', $request->get_route(), $timestamp, $nonce, ''
            ),
        ) as $name => $value) {
            $request->set_header($name, $value);
        }
        return $request;
    }

    function dispatch($request)
    {
        $server = rest_get_server();
        return apply_filters('rest_post_dispatch', $server->dispatch($request), $server, $request);
    }

    function business_request($options, $path, $data)
    {
        $request = new \WP_REST_Request('POST', '/astrbot-zibll/v1' . $path);
        $body = wp_json_encode($data, JSON_UNESCAPED_UNICODE);
        $request->set_body($body);
        $request->set_header('content-type', 'application/json');
        $timestamp = (string) time();
        $nonce = bin2hex(random_bytes(16));
        foreach (array(
            'x-az-client' => $options['client_id'], 'x-az-key' => $options['key_id'],
            'x-az-time' => $timestamp, 'x-az-nonce' => $nonce,
            'x-az-signature' => \AstrBotZibll\signature(
                $options['client_id'], $options['key_id'], $options['secret'],
                'POST', $request->get_route(), $timestamp, $nonce, $body
            ),
        ) as $name => $value) {
            $request->set_header($name, $value);
        }
        return dispatch($request);
    }

    $original_options = $wpdb->options;
    $prefix = 'azt_' . bin2hex(random_bytes(6)) . '_';
    $wpdb->set_prefix($prefix);
    wp_cache_flush();
    $tables = array(
        $wpdb->options, \AstrBotZibll\state_table(), \AstrBotZibll\binding_table(),
        \AstrBotZibll\daily_table(), \AstrBotZibll\operation_table(),
    );
    $GLOBALS['az_test_admin'] = true;
    $GLOBALS['az_test_theme'] = 'zibll';
    $GLOBALS['az_test_missing_functions'] = array();
    $GLOBALS['az_test_theme_options'] = array(
        'checkin_s' => true, 'points_s' => true, 'user_level_s' => true,
    );
    try {
        check($wpdb->query("CREATE TABLE {$wpdb->options} LIKE $original_options") !== false, 'Create isolated options');
        \AstrBotZibll\install();
        check(\AstrBotZibll\settings()['enabled'] === false, 'Disabled by default');
        $autoload = $wpdb->get_var($wpdb->prepare(
            "SELECT autoload FROM {$wpdb->options} WHERE option_name = %s",
            \AstrBotZibll\OPTION
        ));
        check(!in_array($autoload, wp_autoload_values_to_autoload(), true), 'Secret option is not autoloaded');
        add_option('home', 'http://localhost:18797');
        add_option('siteurl', 'http://localhost:18797');
        add_option('timezone_string', 'Asia/Shanghai');
        // Fixture URL changes must not start WordPress HTTPS migration probes.
        remove_all_actions('update_option_home');
        remove_filter('option_home', '_config_wp_home');

        $save = array('_wpnonce' => 'test-csrf', 'az_action' => 'rotate');
        $before = \AstrBotZibll\settings();
        $GLOBALS['az_test_admin'] = false;
        check(error_is(\AstrBotZibll\save_settings($save), 'forbidden', 403), 'Admin capability required');
        check(\AstrBotZibll\settings() === $before, 'Unauthorized save has no changes');
        $GLOBALS['az_test_admin'] = true;
        check(error_is(\AstrBotZibll\save_settings(array_merge($save, array('_wpnonce' => 'wrong'))), 'invalid_nonce', 403), 'CSRF required');
        check(error_is(\AstrBotZibll\save_settings(array_merge($save, array('_wpnonce' => array()))), 'invalid_nonce', 403), 'Reject nonstring nonce');
        $secret = \AstrBotZibll\save_settings($save);
        check(is_string($secret) && strlen($secret) === 64, 'Random 32-byte secret');
        $first = \AstrBotZibll\settings();
        $next_secret = \AstrBotZibll\save_settings($save);
        $second = \AstrBotZibll\settings();
        check($first['client_id'] === $second['client_id'], 'Client identity survives rotation');
        check($first['key_id'] !== $second['key_id'] && $secret !== $next_secret, 'Rotation changes credentials');
        check(error_is(
            \AstrBotZibll\validate_signature(request_for($first), $second, time()),
            'authentication_failed', 401
        ), 'Rotated key is rejected');
        check(\AstrBotZibll\parse_groups('') === array(), 'Empty groups deny business');
        check(\AstrBotZibll\parse_groups("p|001|g\np|001|g") === array('p|001|g'), 'Groups are exact and deduplicated');
        foreach (array('*', 'p|b|', 'p|b|g|s', "p|b|g\0", 'p|b|g space', array()) as $groups) {
            check(error_is(\AstrBotZibll\parse_groups($groups), 'invalid_groups', 400), 'Invalid groups rejected');
        }
        $save['az_action'] = 'save';
        $save['enabled'] = '1';
        check(error_is(\AstrBotZibll\save_settings($save), 'key_and_groups_required', 400), 'Cannot enable empty groups');
        $save['allowed_groups'] = 'p|b|g';
        check(\AstrBotZibll\save_settings($save) === null, 'Enable with valid key and scope');
        $save['az_action'] = 'revoke';
        check(\AstrBotZibll\save_settings($save) === null, 'Revoke credentials');
        $revoked = \AstrBotZibll\settings();
        check(!$revoked['enabled'] && $revoked['secret'] === '', 'Revocation disables authentication');
        check($revoked['client_id'] === $first['client_id'], 'Revocation preserves stable identity');

        $options = array_merge($revoked, array(
            'enabled' => true,
            'client_id' => 'test-client', 'key_id' => 'test-key',
            'secret' => bin2hex(implode('', array_map('chr', range(0, 31)))),
            'allowed_groups' => array('p|b|g'), 'allow_local_http' => false,
        ));
        update_option(\AstrBotZibll\OPTION, $options, false);
        $now = time();
        check(\AstrBotZibll\signature(
            'test-client', 'test-key', $options['secret'], 'POST',
            '/astrbot-zibll/v1/articles/search', '1792584000',
            '0123456789abcdef0123456789abcdef', '{"keyword":"子比"}'
        ) === '1ed994284ea18d59fd0f2b21b84d58c44946154c4003fc0755b011c8ce95cfb2', 'Python/PHP UTF-8 signature vector');
        check(\AstrBotZibll\validate_signature(request_for($options), $options, $now) === true, 'Valid signature');
        foreach (array(-180, 180) as $offset) {
            check(\AstrBotZibll\validate_signature(request_for($options, $now + $offset), $options, $now) === true, 'Inclusive clock boundary');
        }
        foreach (array(-181, 181) as $offset) {
            check(error_is(\AstrBotZibll\validate_signature(request_for($options, $now + $offset), $options, $now), 'authentication_failed', 401), 'Reject expired or future time');
        }
        $mutations = array(
            static function ($r) { $r->set_header('x-az-client', 'other'); },
            static function ($r) { $r->set_header('x-az-key', 'other'); },
            static function ($r) { $r->set_header('x-az-nonce', 'bad'); },
            static function ($r) { $r->set_header('x-az-signature', str_repeat('0', 64)); },
            static function ($r) { $r->set_header('x-az-time', (string) (time() + 1)); },
            static function ($r) { $r->set_header('x-az-client', 'test-client,test-client'); },
            static function ($r) { $r->set_method('POST'); },
            static function ($r) { $r->set_route('/astrbot-zibll/v1/profile'); },
            static function ($r) { $r->set_body('{"uid":1}'); },
        );
        foreach ($mutations as $mutation) {
            $request = request_for($options);
            $mutation($request);
            check(error_is(\AstrBotZibll\validate_signature($request, $options, $now), 'authentication_failed', 401), 'Tampering rejected');
        }
        check(error_is(\AstrBotZibll\validate_signature(new \WP_REST_Request('GET', '/'), $options, $now), 'authentication_failed', 401), 'Missing headers rejected');
        foreach (array(array('uid' => '1'), array('rest_route' => '/other'), array('rest_route' => null)) as $query) {
            $request = request_for($options);
            $request->set_query_params($query);
            check(error_is(\AstrBotZibll\validate_signature($request, $options, $now), 'invalid_parameters', 400), 'Reject unsigned query parameters');
        }
        $request = request_for($options);
        $request->set_query_params(array('rest_route' => $request->get_route()));
        check(\AstrBotZibll\validate_signature($request, $options, $now) === true, 'Canonical rest_route supported');

        $_SERVER['HTTPS'] = 'off';
        check(!\AstrBotZibll\transport_allowed($options), 'HTTP disabled by default');
        $local_options = array_merge($options, array('allow_local_http' => true));
        check(\AstrBotZibll\transport_allowed($local_options), 'Explicit loopback HTTP allowed');
        $_SERVER['HTTP_HOST'] = 'public.example.com';
        check(!\AstrBotZibll\transport_allowed($local_options), 'Public HTTP request rejected');
        $_SERVER['HTTP_HOST'] = 'localhost:18797';
        update_option('home', 'http://public.example.com');
        check(!\AstrBotZibll\transport_allowed($local_options), 'Public website HTTP rejected');
        update_option('home', 'http://localhost:18797');
        $_SERVER['HTTPS'] = 'on';

        $GLOBALS['wp_rest_server'] = new \WP_REST_Server();
        remove_all_actions('rest_api_init');
        add_action('rest_api_init', 'AstrBotZibll\\register_routes');
        do_action('rest_api_init');
        $actor = array('platform_id' => 'p', 'bot_self_id' => 'b', 'group_id' => 'g', 'sender_id' => 's');
        $started = business_request($options, '/binding/start', array('actor' => $actor));
        check($started->get_status() === 200 && isset($started->get_data()['data']['url']), 'Signed actor can start website authorization');
        $request = request_for($options);
        $response = dispatch($request);
        check($response->get_status() === 200, 'Real REST dispatch authenticates');
        $data = $response->get_data();
        check(in_array('binding/start', $data['data']['actions'], true), 'Binding capability advertised');
        check($data['data']['public_site_url'] === 'https://localhost:18797', 'Public site URL follows home and request scheme');
        check($data['data']['timezone'] === 'Asia/Shanghai', 'Site timezone is exposed');
        check(!str_contains(wp_json_encode($data), $options['secret']), 'Response excludes secret');
        check($response->get_headers()['Cache-Control'] === 'no-store, private, max-age=0', 'Success is private/no-store');
        $replayed = dispatch($request);
        check($replayed->get_status() === 409 && $replayed->get_data()['code'] === 'replay', 'REST nonce replay rejected');
        check(isset($replayed->get_data()['request_id']), 'Errors have request IDs');
        check($replayed->get_headers()['Cache-Control'] === 'no-store, private, max-age=0', 'Errors are private/no-store');
        $uppercase = request_for($options);
        $uppercase->set_route('/ASTRBOT-ZIBLL/v1/capabilities');
        $uppercase_response = dispatch($uppercase);
        check($uppercase_response->get_status() === 401, 'Route case tampering fails authentication');
        check($uppercase_response->get_headers()['Cache-Control'] === 'no-store, private, max-age=0', 'Case-variant errors remain private');
        $GLOBALS['az_test_theme'] = 'other';
        check(dispatch(request_for($options))->get_status() === 503, 'Non-Zibll theme fails safely');
        $GLOBALS['az_test_theme'] = 'zibll';
        $GLOBALS['az_test_missing_functions'] = array('zib_user_checkin');
        check(dispatch(request_for($options))->get_status() === 503, 'Missing theme function fails safely');
        $GLOBALS['az_test_missing_functions'] = array();

        $nonce = bin2hex(random_bytes(16));
        check(\AstrBotZibll\consume_nonce('test-client', 'test-key', $nonce, $now) === true, 'First nonce consumption succeeds');
        check(error_is(\AstrBotZibll\consume_nonce('test-client', 'test-key', $nonce, $now + 360), 'replay', 409), 'Future signature cannot replay at far clock boundary');
        $expiry = $wpdb->get_var($wpdb->prepare(
            'SELECT expires_at FROM ' . \AstrBotZibll\state_table() . ' WHERE state_key = %s',
            hash('sha256', "nonce\ntest-client\ntest-key\n$nonce")
        ));
        check((int) $expiry === $now + 361, 'Nonce retained across both clock windows');
        \AstrBotZibll\consume_nonce('test-client', 'test-key', bin2hex(random_bytes(16)), $now + 362);
        check($wpdb->get_var($wpdb->prepare(
            'SELECT COUNT(*) FROM ' . \AstrBotZibll\state_table() . ' WHERE expires_at < %d', $now + 362
        )) === '0', 'Expired nonce state is cleaned in bounded batches');

        $processes = array();
        $nonce = bin2hex(random_bytes(16));
        $start = microtime(true) + 3;
        for ($i = 0; $i < 20; ++$i) {
            $process = proc_open(array(
                PHP_BINARY, __FILE__, '--nonce-worker', $prefix, $nonce, (string) $now, (string) $start,
            ), array(0 => array('pipe', 'r'), 1 => array('pipe', 'w'), 2 => array('pipe', 'w')), $pipes);
            if (!is_resource($process)) {
                throw new \RuntimeException('Cannot start nonce worker.');
            }
            fclose($pipes[0]);
            $processes[] = array($process, $pipes);
        }
        $results = array();
        foreach ($processes as [$process, $pipes]) {
            $results[] = trim(stream_get_contents($pipes[1]));
            $error = stream_get_contents($pipes[2]);
            fclose($pipes[1]);
            fclose($pipes[2]);
            check(proc_close($process) === 0 && $error === '', 'Nonce worker exited cleanly');
        }
        check(count(array_filter($results, static fn($result) => $result === 'ok')) === 1, 'Only one concurrent nonce accepted');
        check(count(array_filter($results, static fn($result) => $result === 'replay')) === 19, 'Other 19 concurrent replays rejected');
        $wpdb->set_prefix($prefix . 'missing_');
        check(error_is(\AstrBotZibll\consume_nonce('a', 'b', 'c', $now), 'unavailable', 503), 'Missing state table fails closed');
        $wpdb->set_prefix($prefix);
        $_SERVER['REQUEST_METHOD'] = 'GET';
        ob_start();
        \AstrBotZibll\admin_page();
        $html = ob_get_clean();
        check(!str_contains($html, $options['secret']), 'Saved key is not redisplayed on GET');
        check(str_contains($html, 'test-client') && str_contains($html, 'test-csrf'), 'Admin form includes identity and CSRF field');
        echo "PASS $checks checks (isolated WordPress REST/wpdb; theme and admin identity doubles)\n";
    } finally {
        foreach ($tables as $table) {
            if (!preg_match('/\Aazt_[0-9a-f]{12}_(options|astrbot_zibll_(state|bindings|daily|operations))\z/', $table)) {
                throw new \RuntimeException('Refusing unsafe test table cleanup.');
            }
            $wpdb->query("DROP TABLE IF EXISTS $table");
        }
        $remaining = $wpdb->get_var($wpdb->prepare(
            'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name LIKE %s',
            $wpdb->esc_like($prefix) . '%'
        ));
        if ($remaining !== '0') {
            throw new \RuntimeException('Temporary table cleanup failed.');
        }
        echo "Temporary tables remaining: 0\n";
    }
}
