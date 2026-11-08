package net.tabularium.agents;
import java.net.URI;

final class OriginPolicy {
    static String origin(String value) {
        try {
            URI uri = new URI(value.trim());
            String host = uri.getHost();
            String[] labels = host == null ? new String[0] : host.toLowerCase(java.util.Locale.ROOT).split("\\.");
            if (!"https".equalsIgnoreCase(uri.getScheme()) || host == null ||
                labels.length < 3 || !labels[labels.length-2].equals("ts") || !labels[labels.length-1].equals("net") ||
                uri.getRawUserInfo() != null ||
                (uri.getPort() != -1 && uri.getPort() != 443)) return null;
            return "https://" + host.toLowerCase(java.util.Locale.ROOT);
        } catch (Exception error) { return null; }
    }
    static boolean allows(String base, String target) {
        String parsed = origin(target);
        return parsed != null && parsed.equals(base);
    }
}
