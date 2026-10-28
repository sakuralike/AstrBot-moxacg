<?php

namespace AstrBotZibll;

defined('ABSPATH') || exit;

function business_routes()
{
    return array(
        'binding/start', 'binding/status', 'binding/confirm', 'binding/revoke',
        'binding/code/start', 'binding/code/redeem', 'binding/code/status',
        'profile', 'checkin', 'checkin/status', 'articles/latest', 'articles/search',
        'points/add', 'points/subtract', 'points/status',
    );
}

function dispatch_business($request)
{
    $actor = actor_data($request);
    if (is_wp_error($actor)) {
        return $actor;
    }
    if (!theme_status()['ready']) {
        return failure('unavailable', 503);
    }
    $action = substr($request->get_route(), strlen(NAMESPACE_PATH) + 1);
    $input = $request->get_json_params();
    if (str_starts_with($action, 'binding/code/')) {
        return binding_code_action($action, $actor, $input);
    }
    if (str_starts_with($action, 'binding/')) {
        return binding_action($action, $actor, $input);
    }
    if (str_starts_with($action, 'articles/')) {
        return articles($action === 'articles/search', $input);
    }
    if (str_starts_with($action, 'points/')) {
        return points_action($action, $actor, $input);
    }
    return with_lock('actor:' . $actor['actor_hash'], static function () use ($action, $actor, $input) {
        $user = bound_user($actor['actor_hash']);
        if (is_wp_error($user)) {
            return $user;
        }
        return $action === 'profile' ? profile($user)
            : checkin($user->ID, $actor['actor_hash'], $input, $action === 'checkin/status');
    });
}

function profile($user)
{
    $uid = (int) $user->ID;
    $days = null;
    $joined = \DateTimeImmutable::createFromFormat('!Y-m-d H:i:s', $user->user_registered, new \DateTimeZone('UTC'));
    if ($joined && $joined->format('Y-m-d H:i:s') === $user->user_registered && $joined->getTimestamp() <= time()) {
        $today = current_datetime()->setTime(0, 0);
        $days = (int) $joined->setTimezone(wp_timezone())->setTime(0, 0)->diff($today)->days + 1;
    }
    $avatar = zib_get_user_avatar_url($uid, 160);
    if (is_string($avatar) && str_starts_with($avatar, '//')) {
        $avatar = wp_parse_url(home_url('/'), PHP_URL_SCHEME) . ':' . $avatar;
    }
    return success(array(
        'uid' => $uid, 'nickname' => $user->display_name,
        'avatar_url' => is_string($avatar) ? esc_url_raw($avatar, array('https', 'http')) : '',
        'joined_days' => $days,
        'level' => _pz('user_level_s', true)
            ? min(max(1, (int) get_user_meta($uid, 'level', true)), max(1, (int) _pz('user_level_max', 10))) : null,
        'points' => _pz('points_s') ? (int) zibpay_get_user_points($uid) : null,
        'post_count' => (int) zib_get_user_post_count($uid, 'publish', 'post'),
        'comment_count' => (int) get_user_comment_count($uid, 'approve', false),
    ));
}

function articles($search, $input)
{
    global $wpdb;
    $count = $input['count'] ?? 5;
    $keyword = $input['keyword'] ?? '';
    if (!is_int($count) || $count < 1 || $count > 10
        || ($search && (!is_string($keyword) || !preg_match('/\A[^\p{C}]{1,60}\z/u', $keyword) || trim($keyword) === ''))
    ) {
        return failure('invalid_parameters', 400);
    }
    $original_user = get_current_user_id();
    wp_set_current_user(0);
    try {
        $items = array();
        $excluded = array_merge((array) _pz('home_exclude_cats', array()), (array) _pz('search_exclude_cats', array()));
        if (_pz('docs_mode_exclude')) {
            $excluded = array_merge($excluded, (array) _pz('docs_mode_cats', array()));
        }
        foreach ($excluded as $category) {
            $children = get_term_children((int) $category, 'category');
            if (!is_wp_error($children)) {
                $excluded = array_merge($excluded, $children);
            }
        }
        for ($page = 1; $page <= 3 && count($items) < $count; ++$page) {
            $args = array(
                'post_type' => 'post', 'post_status' => 'publish', 'has_password' => false,
                'posts_per_page' => $count, 'paged' => $page, 'ignore_sticky_posts' => true,
                'no_found_rows' => true, 'suppress_filters' => false,
                'orderby' => array('date' => 'DESC', 'ID' => 'DESC'),
                'post__not_in' => array_map('intval', (array) _pz('home_exclude_posts', array())),
                'category__not_in' => array_unique(array_map('intval', $excluded)),
            );
            $query = new \WP_Query();
            $title_where = static function ($where, $current) use ($query, $keyword, $wpdb) {
                return $current === $query
                    ? $where . $wpdb->prepare(" AND {$wpdb->posts}.post_title LIKE %s", '%' . $wpdb->esc_like($keyword) . '%')
                    : $where;
            };
            $title_order = static function ($order, $current) use ($query, $keyword, $wpdb) {
                return $current === $query ? $wpdb->prepare(
                    "({$wpdb->posts}.post_title = %s) DESC, ({$wpdb->posts}.post_title LIKE %s) DESC, ",
                    $keyword, $wpdb->esc_like($keyword) . '%'
                ) . $order : $order;
            };
            if ($search) {
                add_filter('posts_where', $title_where, PHP_INT_MAX, 2);
                add_filter('posts_orderby', $title_order, PHP_INT_MAX, 2);
            }
            try {
                $query->query($args);
            } finally {
                if ($search) {
                    remove_filter('posts_where', $title_where, PHP_INT_MAX);
                    remove_filter('posts_orderby', $title_order, PHP_INT_MAX);
                }
            }
            foreach ($query->posts as $post) {
                if ($post->post_status !== 'publish' || $post->post_type !== 'post'
                    || $post->post_password !== '' || !is_post_publicly_viewable($post)
                    || !apply_filters('astrbot_zibll_article_visible', true, $post)
                ) {
                    continue;
                }
                $item = array(
                    'title' => wp_strip_all_tags(html_entity_decode(get_the_title($post), ENT_QUOTES | ENT_HTML5, 'UTF-8')),
                    'published_at' => get_post_time('Y-m-d H:i', false, $post),
                    'url' => get_permalink($post),
                );
                if ($search) {
                    $had_post = array_key_exists('post', $GLOBALS);
                    $original_post = $GLOBALS['post'] ?? null;
                    $GLOBALS['post'] = $post;
                    try {
                        $cover = function_exists('zib_post_thumbnail')
                            ? zib_post_thumbnail('medium', '', true, $post)
                            : get_the_post_thumbnail_url($post, 'medium');
                    } finally {
                        if ($had_post) {
                            $GLOBALS['post'] = $original_post;
                        } else {
                            unset($GLOBALS['post']);
                        }
                    }
                    if (is_string($cover) && str_starts_with($cover, '//')) {
                        $cover = wp_parse_url(home_url('/'), PHP_URL_SCHEME) . ':' . $cover;
                    }
                    $item['cover_url'] = is_string($cover) && strlen($cover) <= 2048
                        ? esc_url_raw($cover, array('https', 'http')) : '';
                }
                $items[] = $item;
                if (count($items) >= $count) {
                    break;
                }
            }
            if (count($query->posts) < $count) {
                break;
            }
        }
        return success(array('items' => $items));
    } finally {
        wp_set_current_user($original_user);
    }
}
