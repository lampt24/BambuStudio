// PrintPilotConfig.cpp
#include "PrintPilotConfig.hpp"

#include <fstream>
#include <boost/filesystem/path.hpp>
#include <boost/filesystem/operations.hpp>
#include <boost/log/trivial.hpp>

#include "libslic3r/Utils.hpp"          // data_dir()
#include "nlohmann/json.hpp"

namespace Slic3r { namespace GUI { namespace AI {

bool PrintPilotConfig::valid() const
{
    const bool scheme = server_url.rfind("https://", 0) == 0 || server_url.rfind("http://", 0) == 0;
    const auto host_start = server_url.find("://");
    return scheme && host_start != std::string::npos &&
           server_url.size() > host_start + 3 && server_url.back() != '/' &&
           server_url.find_first_of("/\\?#@ \t\r\n", host_start + 3) == std::string::npos &&
           !token.empty() && token.find_first_of("\r\n") == std::string::npos;
}

bool PrintPilotConfig::load()
{
    boost::filesystem::path cfg_path =
        boost::filesystem::path(Slic3r::data_dir()) / "printpilot.json";

    boost::system::error_code ec;
    if (!boost::filesystem::exists(cfg_path, ec) || ec)
        return false;

    std::ifstream f(cfg_path.string());
    if (!f.is_open())
        return false;

    try {
        nlohmann::json j;
        f >> j;
        server_url = j.value("server_url", std::string{});
        token      = j.value("token",      std::string{});
    } catch (const std::exception& ex) {
        BOOST_LOG_TRIVIAL(warning) << "PrintPilot: failed to parse printpilot.json: " << ex.what();
        return false;
    }

    if (!valid()) {
        BOOST_LOG_TRIVIAL(warning) << "PrintPilot: printpilot.json missing server_url or token";
        return false;
    }
    return true;
}

}}} // namespace Slic3r::GUI::AI
